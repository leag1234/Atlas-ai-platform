"""Owner 2026-10-21: visual requests execute tools and display local assets."""

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image


class CapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_cases(self) -> None:
        from evals.capability.harness import CASES, assess, offline

        with tempfile.TemporaryDirectory() as root:
            for case in json.loads(CASES.read_text()):
                with self.subTest(case=case["id"]):
                    answer, events, assets = await offline(case, Path(root))
                    self.assertTrue(assess(case, answer, events, assets)["passed"])

    async def test_private_image_addresses_refused(self) -> None:
        from services.orchestrator.web import Web

        for url in (
            "http://127.0.0.1/x.png",
            "http://169.254.169.254/x",
            "http://[::1]/x",
        ):
            with self.assertRaisesRegex(ValueError, "ssrf_denied"):
                await Web().fetch_image(url)

    def test_qr_and_image_validation(self) -> None:
        from services.orchestrator.visual_tools import MakeQR, make_qr, normalize_image
        from services.orchestrator.image_store import load_uploaded
        import base64

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict("os.environ", {"ATLAS_IMAGE_DIR": root}),
        ):
            result = make_qr(MakeQR(payload="https://example.org", foreground="blue"))
            raw = base64.b64decode(
                load_uploaded(str(result["reference"])).split(",")[1]
            )
            with Image.open(io.BytesIO(raw)) as picture:
                self.assertGreaterEqual(min(picture.size), 256)
                self.assertIn((0, 0, 255), picture.convert("RGB").getdata())
            with self.assertRaisesRegex(ValueError, "content_type"):
                normalize_image(raw, "text/html")
            with self.assertRaisesRegex(ValueError, "too_large"):
                normalize_image(b"x" * (5 * 1024 * 1024 + 1), "image/png")
            with self.assertRaises(ValueError):
                make_qr(MakeQR(payload="x", foreground="white", background="white"))

    async def test_provider_failure_has_status(self) -> None:
        from decimal import Decimal
        from services.orchestrator.cache import Cache
        from services.orchestrator.loop import Call
        from services.orchestrator.tools import Runtime
        from services.orchestrator.visual_tools import ImageProviderError

        with tempfile.TemporaryDirectory() as root:
            runtime = Runtime(Cache(Path(root) / "cache"), Decimal(0))
            with patch(
                "services.orchestrator.visual_tools.search_images",
                new=AsyncMock(
                    side_effect=ImageProviderError("tavily", 432, "quota_exceeded")
                ),
            ):
                result = await runtime.execute(
                    Call("x", "web_images", '{"query":"cats","count":5}'), 10
                )
            self.assertEqual(result["provider_status"], 432)
            self.assertEqual(result["error"], "quota_exceeded")

    def test_negative_routes(self) -> None:
        from services.orchestrator.capability_strategy import strategy

        for question in (
            "What is a QR code?",
            "Do not generate a QR code",
            "Explain Art Deco posters",
            "How do I search images on the web?",
            "Ne génère pas de QR code",
        ):
            self.assertIsNone(strategy(question))

    async def test_pipeline_routes_before_model_and_guest_restriction(self) -> None:
        from services.orchestrator.chat_pipeline import process
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.interactions import Interaction

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                "os.environ",
                {
                    "ATLAS_IMAGE_DIR": root,
                    "ATLAS_WEB_CACHE": root + "/cache",
                    "ATLAS_STABLE": "1",
                },
            ),
            patch(
                "services.orchestrator.chat_pipeline.GatewayModel.connect",
                new=AsyncMock(side_effect=AssertionError("no model needed")),
            ),
        ):
            item = Interaction()
            await asyncio.wait_for(
                process(
                    ChatRequest.model_validate(
                        {
                            "messages": [
                                {
                                    "role": "user",
                                    "content": "génère un QR code pour https://example.org en bleu",
                                }
                            ]
                        }
                    ),
                    item,
                ),
                5,
            )
            self.assertIn("![", item.reponse)
            self.assertIn("/images/", item.reponse)
            self.assertNotIn("[unverified source]", item.reponse)
            self.assertEqual(item.state, "done")

    async def test_web_images_no_hotlinks_deduplication_and_provenance(self) -> None:
        from decimal import Decimal
        from services.orchestrator.cache import Cache
        from services.orchestrator.loop import Call
        from services.orchestrator.tools import Runtime
        from services.orchestrator.web import Web

        raw = io.BytesIO()
        Image.new("RGB", (100, 100), "blue").save(raw, format="PNG")
        candidates = {
            "candidates": [
                {
                    "url": "https://example.org/image",
                    "source_url": "https://example.org/page",
                },
                {
                    "url": "https://example.org/duplicate",
                    "source_url": "https://example.org/page",
                },
                {
                    "url": "http://127.0.0.1/private",
                    "source_url": "https://example.org/page",
                },
            ]
        }
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                "os.environ",
                {
                    "ATLAS_IMAGE_DIR": root + "/images",
                    "ATLAS_SEARCH_PROVIDER": "tavily",
                },
            ),
        ):
            runtime = Runtime(Cache(Path(root) / "cache"), Decimal(0))
            with (
                patch(
                    "services.orchestrator.visual_tools.search_images",
                    new=AsyncMock(return_value=candidates),
                ),
                patch.object(
                    Web,
                    "fetch_image",
                    new=AsyncMock(
                        return_value=("https://example.org/image", raw.getvalue())
                    ),
                ) as fetch,
                patch.object(
                    Web, "fetch", new=AsyncMock(side_effect=ValueError("robots_denied"))
                ),
            ):
                result = await runtime.execute(
                    Call("x", "web_images", '{"query":"cats","count":3}'), 10
                )
            data = result["data"]
            assert isinstance(data, dict)
            self.assertEqual(data["found"], 1)
            self.assertEqual(fetch.await_count, 2)
            self.assertNotIn("](https://example.org/page)", str(data["markdown"]))
            self.assertNotIn("![Web image 1](https://", str(data["markdown"]))
            self.assertIn("/images/", str(data["markdown"]))

    async def test_guest_daily_and_global_search_quotas(self) -> None:
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.daily_budget import DailyLedger
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Call

        for setting in ("ATLAS_USER_DAILY_SEARCHES", "ATLAS_GLOBAL_DAILY_SEARCHES"):
            with (
                self.subTest(setting=setting),
                tempfile.TemporaryDirectory() as root,
                patch.dict(
                    "os.environ",
                    {
                        "ATLAS_WEB_CACHE": root + "/cache",
                        "ATLAS_SEARCH_PROVIDER": "tavily",
                        setting: "1",
                    },
                ),
            ):
                runtime = ChatTools(Interaction())
                runtime.daily_ledger = DailyLedger(Path(root) / "ledger")
                runtime.daily_ledger.reserve_search("unknown", "en", "tavily")
                with patch(
                    "services.orchestrator.visual_tools.search_images", new=AsyncMock()
                ) as search:
                    result = await runtime.execute(
                        Call("x", "web_images", '{"query":"cats"}'), 10
                    )
                search.assert_not_called()
                self.assertEqual(result["error"], "daily_search_limit")
                self.assertIn("2 of 1", str(result["message"]))

    def test_provider_status_classification(self) -> None:
        from services.orchestrator.visual_tools import (
            ImageProviderError,
            provider_status,
        )

        for provider, status, body, code in (
            ("tavily", 432, {}, "quota_exceeded"),
            (
                "serpapi",
                429,
                {"error": "You have run out of searches"},
                "quota_exceeded",
            ),
            ("serpapi", 429, {"error": "hourly throughput"}, "search_rate_limited"),
            ("tavily", 503, {}, "image_search_provider_failed"),
        ):
            with (
                self.subTest(provider=provider, status=status),
                self.assertRaises(ImageProviderError) as caught,
            ):
                provider_status(provider, status, body)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.status, status)

    async def test_redirect_and_dns_private_refused_for_images(self) -> None:
        from services.orchestrator.web import Web

        with (
            patch.object(Web, "_robots", new=AsyncMock()),
            patch.object(
                Web,
                "_request",
                new=AsyncMock(return_value=(302, "http://10.0.0.1/x", b"")),
            ),
            self.assertRaisesRegex(ValueError, "ssrf_denied"),
        ):
            await Web().fetch_image("https://example.org/image")
        with (
            patch(
                "aiohttp.resolver.ThreadedResolver.resolve",
                new=AsyncMock(return_value=[{"host": "192.168.1.1"}]),
            ),
            self.assertRaisesRegex(ValueError, "ssrf_denied"),
        ):
            await Web().fetch_image("https://example.org/image")

    def test_image_dimensions_and_corrupt_bytes(self) -> None:
        from services.orchestrator.visual_tools import normalize_image

        for size in ((16, 16), (8193, 32), (4096, 4096)):
            raw = io.BytesIO()
            Image.new("RGB", size).save(raw, format="PNG")
            with self.assertRaisesRegex(ValueError, "dimensions_rejected"):
                normalize_image(raw.getvalue(), "image/png")
        with self.assertRaisesRegex(ValueError, "invalid_image"):
            normalize_image(b"not an image", "image/png")

    async def test_image_http_headers_and_streamed_caps(self) -> None:
        from aiohttp import web
        from services.orchestrator.web import PublicResolver, Web

        mode = "valid"
        raw = io.BytesIO()
        Image.new("RGB", (100, 100), "blue").save(raw, format="PNG")

        async def handler(request: web.Request) -> web.StreamResponse:
            if request.path == "/robots.txt":
                return web.Response(text="User-agent: *\nAllow: /\n")
            if mode == "type":
                return web.Response(body=raw.getvalue(), content_type="text/html")
            if mode == "size":
                return web.Response(
                    body=b"x" * (5 * 1024 * 1024 + 1), content_type="image/png"
                )
            if mode == "chunked":
                response = web.StreamResponse(headers={"Content-Type": "image/png"})
                response.enable_chunked_encoding()
                await response.prepare(request)
                try:
                    for _ in range(321):
                        await response.write(b"x" * 16384)
                except ConnectionResetError:
                    return response
                return response
            return web.Response(body=raw.getvalue(), content_type="image/png")

        app = web.Application()
        app.router.add_get("/{path:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]

        async def resolve(
            self_: PublicResolver, host: str, port_: int = 0, family: int = 0
        ) -> list[dict[str, object]]:
            return [
                {
                    "hostname": host,
                    "host": "127.0.0.1",
                    "port": port,
                    "family": 2,
                    "proto": 0,
                    "flags": 0,
                }
            ]

        try:
            with patch.object(PublicResolver, "resolve", resolve):
                self.assertEqual(
                    (await Web().fetch_image("http://public.example/image"))[1],
                    raw.getvalue(),
                )
                for mode, error in (
                    ("type", "content_type"),
                    ("size", "response_too_large"),
                    ("chunked", "response_too_large"),
                ):
                    with (
                        self.subTest(mode=mode),
                        self.assertRaisesRegex(ValueError, error),
                    ):
                        await Web().fetch_image("http://public.example/image")
        finally:
            await runner.cleanup()

    async def test_combined_search_limit(self) -> None:
        from decimal import Decimal
        from services.orchestrator.loop import (
            Call,
            Limits,
            Query,
            Reservation,
            Turn,
            run,
        )
        from unittest.mock import Mock

        class Model:
            def estimate(self, messages: list[dict[str, object]]) -> Reservation:
                return Reservation(0, Decimal(0))

            async def complete(
                self, messages: list[dict[str, object]], timeout: float
            ) -> Turn:
                return Turn(
                    "",
                    tuple(
                        Call(str(i), name, json.dumps({"query": str(i)}))
                        for i, name in enumerate(
                            ("web_search", "web_images", "web_search", "web_images")
                        )
                    ),
                )

        runtime = Mock()
        runtime.estimate.return_value = Reservation(0, Decimal(0))
        runtime.execute = AsyncMock(return_value={"data": {}})
        result = await run(
            Query(question="images", lang="en"), Model(), runtime, "", Limits()
        )
        self.assertEqual(result.reason, "search_limit")
        self.assertEqual(runtime.execute.await_count, 3)

    def test_provider_image_source_association(self) -> None:
        from services.orchestrator.visual_tools import image_candidates

        actual = image_candidates(
            {
                "images": [{"url": "https://cdn.example.org/unattributed"}],
                "results": [
                    {
                        "url": "https://example.org/page",
                        "images": [
                            {
                                "url": "https://cdn.example.org/photo",
                                "description": "photo",
                            }
                        ],
                    }
                ],
            },
            "tavily",
        )
        self.assertEqual(
            actual["candidates"],
            [
                {
                    "url": "https://cdn.example.org/photo",
                    "source_url": "https://example.org/page",
                }
            ],
        )
        actual = image_candidates(
            {
                "images_results": [
                    {
                        "original": "https://cdn.example.org/photo",
                        "link": "https://example.org/page",
                    }
                ]
            },
            "serpapi",
        )
        self.assertEqual(
            actual["candidates"],
            [
                {
                    "url": "https://cdn.example.org/photo",
                    "source_url": "https://example.org/page",
                }
            ],
        )

    async def test_stream_preserves_actual_qr_payload(self) -> None:
        from time import monotonic
        from services.orchestrator.chat_pipeline import process
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.chat_stream import response
        from services.orchestrator.interactions import Interaction

        request = ChatRequest.model_validate(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "Generate a blue QR code for https://example.org",
                    }
                ]
            }
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                "os.environ",
                {
                    "ATLAS_IMAGE_DIR": root + "/images",
                    "ATLAS_WEB_CACHE": root + "/cache",
                },
            ),
            patch("services.orchestrator.chat_stream.write_interaction"),
        ):
            stream = response(request, Interaction(), monotonic(), process)
            chunks = [str(chunk) async for chunk in stream.body_iterator]
        output = "".join(chunks)
        self.assertIn("https://example.org", output)
        self.assertIn("/images/", output)
        self.assertNotIn("unverified source", output)
