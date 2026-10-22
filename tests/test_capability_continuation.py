"""Owner 2026-10-21: generation/search boundary and localized QR categories."""

import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator.capability_strategy import finish, qr_examples, strategy
from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call
from services.orchestrator.visual_copy import copy
from services.orchestrator.visual_tools import MakeQR, make_qr


class ContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_route_boundaries(self) -> None:
        cases = (
            ("génère une image d'un chat", "generation"),
            ("trouve des photos de chats sur le web", "web_images"),
            ("fais-moi un QR code pour https://example.org", "make_qr"),
            ("cherche des qr code variés sur le web et affiche les ici", "make_qr"),
        )
        for question, expected in cases:
            with (
                self.subTest(question=question),
                patch.dict("os.environ", {"ATLAS_STABLE": "0"}),
                patch(
                    "services.orchestrator.chat_pipeline.process_image", new=AsyncMock()
                ) as generate,
                patch(
                    "services.orchestrator.capability_strategy.finish", new=AsyncMock()
                ) as visual,
                patch(
                    "services.orchestrator.chat_pipeline.GatewayModel.connect",
                    new=AsyncMock(side_effect=AssertionError("unexpected model")),
                ),
            ):
                request = ChatRequest.model_validate(
                    {"messages": [{"role": "user", "content": question}]}
                )
                await process(request, Interaction())
                if expected == "generation":
                    generate.assert_awaited_once()
                    assert generate.await_args is not None
                    self.assertEqual(generate.await_args.args[0], question)
                    visual.assert_not_awaited()
                else:
                    generate.assert_not_awaited()
                    visual.assert_awaited_once()
                    assert visual.await_args is not None
                    self.assertTrue(
                        all(call.name == expected for call in visual.await_args.args[2])
                    )

    def test_exact_payloads_and_generation_exclusion(self) -> None:
        for question, expected in (
            ('Make a QR code for "Hello, world!"', ["Hello, world!"]),
            ("fais-moi un QR code pour Bonjour le monde", ["Bonjour le monde"]),
            (
                "Make QR codes for https://example.org/a and https://example.org/b",
                ["https://example.org/a", "https://example.org/b"],
            ),
            ("Create a QR for mailto:alex@example.org", ["mailto:alex@example.org"]),
        ):
            calls = strategy(question)
            self.assertIsNotNone(calls)
            self.assertEqual(
                [json.loads(call.arguments)["payload"] for call in calls or ()],
                expected,
            )
        self.assertIsNone(strategy("generate an image of a cat for my web page"))

    async def test_localized_category_content_and_many(self) -> None:
        for language in ("fr", "en", "de", "es", "it"):
            for many in (False, True):
                with (
                    self.subTest(language=language, many=many),
                    tempfile.TemporaryDirectory() as root,
                    patch.dict(
                        "os.environ",
                        {
                            "ATLAS_IMAGE_DIR": root,
                            "ATLAS_WEB_CACHE": root + "/cache",
                            "ATLAS_TERMINAL_ENABLED": "0",
                        },
                    ),
                ):
                    question = (
                        "Show "
                        + ("many " if many else "")
                        + "QR codes for different categories to test"
                    )
                    calls = strategy(question)
                    assert calls is not None
                    request = ChatRequest.model_validate(
                        {
                            "messages": [{"role": "user", "content": question}],
                            "lang": language,
                        }
                    )
                    item = Interaction()
                    await finish(request, item, calls)
                    self.assertEqual(len(item.images), 16 if many else 8)
                    self.assertEqual(
                        len({row["reference"] for row in item.images}), len(item.images)
                    )
                    self.assertIn(copy(language, "examples"), item.reponse)
                    self.assertIn(copy(language, "categories"), item.reponse)
                    self.assertIn(copy(language, "payload"), item.reponse)
                    for category, payloads in qr_examples().items():
                        self.assertIn(copy(language, category), item.reponse)
                        for payload in payloads if many else payloads[:1]:
                            self.assertIn(payload, item.reponse)
                    if language != "en":
                        self.assertNotIn("Generated locally", item.reponse)
                        self.assertNotIn("Payload:", item.reponse)

    async def test_localized_partial_results_and_errors(self) -> None:
        for language in ("fr", "en", "de", "es", "it"):
            request = ChatRequest.model_validate(
                {
                    "messages": [{"role": "user", "content": "Find cat photos online"}],
                    "lang": language,
                }
            )
            for output, expected in (
                (
                    {"data": {"markdown": "", "found": 1, "requested": 5}},
                    copy(language, "partial").format(found=1, requested=5),
                ),
                (
                    {
                        "error": "quota_exceeded",
                        "provider": "serpapi",
                        "provider_status": 429,
                    },
                    copy(language, "quota") + " (serpapi HTTP 429)",
                ),
                ({"error": "visual_request_timeout"}, copy(language, "timeout")),
                (
                    {"error": "invalid_arguments", "message": "English diagnostic"},
                    copy(language, "invalid"),
                ),
            ):
                with patch(
                    "services.orchestrator.chat_pipeline.ChatTools.execute",
                    new=AsyncMock(return_value=output),
                ):
                    item = Interaction()
                    await finish(request, item, (Call("test", "web_images", "{}"),))
                    self.assertEqual(item.reponse.strip(), expected)

    def test_tool_level_localization_and_precise_count(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict("os.environ", {"ATLAS_IMAGE_DIR": root}),
        ):
            for language in ("fr", "en", "de", "es", "it"):
                result = make_qr(MakeQR(payload="https://example.org"), language)
                assert isinstance(result["images"], list)
                assert isinstance(result["markdown"], str)
                self.assertEqual(len(result["images"]), 1)
                self.assertIn(copy(language, "qr"), result["markdown"])
                self.assertIn(copy(language, "payload"), result["markdown"])
                self.assertIn("https://example.org", result["markdown"])

    async def test_web_credit_localization_and_retrieval(self) -> None:
        from decimal import Decimal
        from pathlib import Path
        import io
        from PIL import Image
        from services.orchestrator.cache import Cache
        from services.orchestrator.tools import Runtime

        raw = io.BytesIO()
        Image.new("RGB", (128, 128), "green").save(raw, format="PNG")
        for language in ("fr", "en", "de", "es", "it"):
            with (
                tempfile.TemporaryDirectory() as root,
                patch.dict(
                    "os.environ",
                    {"ATLAS_IMAGE_DIR": root, "ATLAS_SEARCH_PROVIDER": "serpapi"},
                ),
            ):
                runtime = Runtime(Cache(Path(root) / "cache"), Decimal(0))
                runtime.daily_lang = language
                with (
                    patch(
                        "services.orchestrator.visual_tools.search_images",
                        new=AsyncMock(
                            return_value={
                                "candidates": [
                                    {
                                        "url": "https://example.org/image.png",
                                        "source_url": "https://example.org/page",
                                    }
                                ]
                            }
                        ),
                    ),
                    patch.object(
                        runtime.web,
                        "fetch_image",
                        new=AsyncMock(
                            return_value=(
                                "https://example.org/image.png",
                                raw.getvalue(),
                            )
                        ),
                    ),
                    patch.object(
                        runtime.web,
                        "fetch",
                        new=AsyncMock(
                            return_value=("https://example.org/page", b"page")
                        ),
                    ) as fetched,
                ):
                    result = await runtime.execute(
                        Call("test", "web_images", '{"query":"cats","count":1}'), 10
                    )
                assert isinstance(result["data"], dict)
                self.assertIn(copy(language, "image"), result["data"]["markdown"])
                self.assertIn(copy(language, "source"), result["data"]["markdown"])
                self.assertIn("https://example.org/page", result["data"]["markdown"])
                fetched.assert_awaited_once()

    def test_localized_measured_limits(self) -> None:
        from services.orchestrator.visual_copy import failure

        for language in ("fr", "en", "de", "es", "it"):
            message = failure(
                language, "daily_search_limit", "search limit reached today (2 of 1)"
            )
            self.assertIn("2", message)
            self.assertIn("1", message)
            self.assertIn(copy(language, "quota"), message)
            self.assertNotIn("today", message)
