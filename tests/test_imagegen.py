"""M13 synthetic-only transport tests; no cloud or live inference."""

import base64
import io
import os
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, AsyncMock
from decimal import Decimal
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image
import imagegen
import aiohttp
from packages.imagegen import ImagePrompt, image_request
from packages.image_policy import ImagePolicy
from packages.ports import atlas_ports
from services.orchestrator.imagegen import process_image, startup_failure
from services.orchestrator.interactions import Interaction, write_interaction
from services.orchestrator.model import GatewayError, GatewayModel
from services.orchestrator.chat_api import app
from services.orchestrator.chat_schema import ChatRequest
from fastapi.testclient import TestClient


def png(size: int = 1024) -> str:
    output = io.BytesIO()
    Image.new("RGB", (size, size), "red").save(output, "PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


class ImageTests(unittest.IsolatedAsyncioTestCase):
    def test_input_and_intent(self) -> None:
        fixture_policy = ImagePolicy(
            extended_request_patterns={"en": [r"\bfixture-extended-request\b"]},
            extended_instruction="FIXTURE EXTENDED INSTRUCTION",
        )
        for text in (
            "",
            " ",
            "x" * 2001,
            "fixture-extended-request",
            "x\x00",
        ):
            with (
                self.subTest(text=text[:20]),
                patch("packages.image_limits.POLICY", fixture_policy),
                self.assertRaises(ValueError),
            ):
                ImagePrompt(prompt=text)
        with (
            patch("packages.imagegen.image_limit", return_value="minors"),
            self.assertRaisesRegex(ValueError, "image_content_refused:minors"),
        ):
            ImagePrompt(prompt="ordinary request")
        for text in ("génère une image de cube", "Generate an image of a cube"):
            self.assertTrue(image_request(text))
        self.assertFalse(image_request("Décris cette image"))
        self.assertEqual(ImagePrompt(prompt="un cube rouge").prompt, "un cube rouge")

    async def test_budget_precedes_transport(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"ATLAS_IMAGE_GPU_IP": "127.0.0.1", "ATLAS_IMAGE_GPU_EUR_H": "2.01"},
            ),
            patch.object(aiohttp, "ClientSession") as transport,
        ):
            with self.assertRaisesRegex(RuntimeError, "cost_budget"):
                await imagegen.complete({"prompt": "a cube"})
            transport.assert_not_called()

    async def test_real_http_png_boundary(self) -> None:
        import json

        payload = {"image": png(), "seconds": 0.1, "seed": 123, "steps": 4}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert data == {"prompt": "Subjects: a cube", "seed": None}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

        # Real local HTTP exercises bounded reads and PNG decoding.
        with ThreadingHTTPServer(
            ("127.0.0.2", atlas_ports().port("terminal")), Handler
        ) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with (
                    patch.object(
                        imagegen, "rewrite", AsyncMock(return_value="Subjects: a cube")
                    ),
                    patch.object(imagegen, "reservation", return_value=Decimal("0.01")),
                    patch.dict(
                        os.environ,
                        {
                            "ATLAS_IMAGE_GPU_IP": "127.0.0.2",
                            "ATLAS_IMAGE_GPU_EUR_H": "1.46988",
                        },
                    ),
                ):
                    result = await imagegen.complete({"prompt": "a cube"})
                    self.assertEqual(result["image"], png())
                    self.assertGreater(float(str(result["cost_eur"])), 0)
                    payload["image"] = png(8)
                    with self.assertRaisesRegex(ValueError, "invalid_provider_image"):
                        await imagegen.complete({"prompt": "a cube"})
            finally:
                server.shutdown()
                thread.join()

    async def test_chat_returns_image(self) -> None:
        item = Interaction()
        with patch.object(
            GatewayModel, "post", return_value={"image": png(), "cost_eur": 0.002}
        ) as transport:
            await process_image("génère une image de cube", item, language="fr")
        self.assertEqual(item.task_type, "imagegen")
        self.assertEqual(item.state, "done")
        self.assertNotIn("base64", item.reponse)
        from services.orchestrator.image_store import generated_image

        key = item.reponse.rsplit("/", 1)[1].removesuffix(")")
        self.assertEqual(
            generated_image(key).body, base64.b64decode(png().split(",", 1)[1])
        )
        self.assertTrue(transport.call_args.args[0].endswith("/images/generate"))

    def test_startup_failure_is_final_and_localized_in_five_languages(self) -> None:
        expected = {
            "fr": "n’a pas pu démarrer",
            "en": "could not start",
            "de": "konnte nicht starten",
            "es": "No se pudo iniciar",
            "it": "Non è stato possibile avviare",
        }
        for language, phrase in expected.items():
            with self.subTest(language=language):
                message = startup_failure(language, "provider_error", 503, 1)
                self.assertIn(phrase, message)
                self.assertIn("503", message)
                self.assertIn("0", message)
                self.assertNotIn("SCW_", message)

    async def test_on_demand_provider_failure_retries_then_finishes_message(
        self,
    ) -> None:
        item = Interaction()
        unavailable = GatewayError("provider_error", 503)
        with (
            patch.dict(os.environ, {"ATLAS_IMAGE_ON_DEMAND": "1"}),
            patch.object(
                GatewayModel,
                "post",
                side_effect=[{"ready": False}, unavailable, unavailable],
            ) as transport,
            patch("services.orchestrator.imagegen.asyncio.sleep", AsyncMock()) as sleep,
        ):
            await process_image("generate a cube", item, language="en")
        self.assertEqual(transport.call_count, 3)
        sleep.assert_awaited_once_with(20)
        self.assertEqual(item.provider_retries, 1)
        self.assertEqual(item.state, "done")
        self.assertEqual(item.cout_eur, 0.0)
        self.assertIn("HTTP 503", item.reponse)
        self.assertIn("nothing was charged", item.reponse)

    async def test_on_demand_configuration_timeout_and_unready_are_final(self) -> None:
        cases = (
            (
                GatewayError(
                    "timeout", 504, "Timeout: 895 seconds elapsed, limit 895 seconds"
                ),
                "timeout after 895 seconds",
            ),
            (TimeoutError(), "timeout after"),
        )
        for failure, phrase in cases:
            with self.subTest(failure=type(failure).__name__):
                item = Interaction()
                replies = [{"ready": False}, failure]
                if (
                    isinstance(failure, GatewayError)
                    and failure.status in {502, 503, 504}
                    and failure.code != "configuration_missing"
                ):
                    replies.append(failure)
                with (
                    patch.dict(os.environ, {"ATLAS_IMAGE_ON_DEMAND": "1"}),
                    patch.object(
                        GatewayModel,
                        "post",
                        side_effect=replies,
                    ),
                    patch("services.orchestrator.imagegen.asyncio.sleep", AsyncMock()),
                ):
                    await process_image("generate a cube", item, language="en")
                self.assertEqual(item.state, "done")
                self.assertEqual(item.cout_eur, 0.0)
                self.assertIn(phrase, item.reponse)

    async def test_missing_configuration_keeps_the_explicit_owner_diagnostic(
        self,
    ) -> None:
        item = Interaction()
        failure = GatewayError(
            "configuration_missing",
            503,
            "Missing required environment variables: LOCAL_MODEL",
        )
        with (
            patch.dict(os.environ, {"ATLAS_IMAGE_ON_DEMAND": "1"}),
            patch.object(GatewayModel, "post", side_effect=[{"ready": False}, failure]),
            self.assertRaises(GatewayError) as raised,
        ):
            await process_image("generate a cube", item, language="en")
        self.assertEqual(raised.exception.code, "configuration_missing")

    async def test_on_demand_success_still_generates_the_image(self) -> None:
        item = Interaction()
        with (
            patch.dict(os.environ, {"ATLAS_IMAGE_ON_DEMAND": "1"}),
            patch.object(
                GatewayModel,
                "post",
                side_effect=[
                    {"ready": False},
                    {"ready": True},
                    {"image": png(), "cost_eur": 0.002},
                ],
            ),
        ):
            await process_image("generate a cube", item, language="en")
        self.assertEqual(item.state, "done")
        self.assertIn("![", item.reponse)

    def test_start_failure_is_terminal_content_for_json_and_sse(self) -> None:
        async def fail_image(request: ChatRequest, item: Interaction) -> None:
            await process_image("generate a cube", item, language=request.lang)

        unavailable = GatewayError("provider_error", 503)
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.dict(
                    os.environ,
                    {
                        "ATLAS_IMAGE_ON_DEMAND": "1",
                        "ATLAS_INTERACTION_DIR": directory,
                    },
                ),
                TestClient(app) as client,
                patch("services.orchestrator.chat_api.process", side_effect=fail_image),
                patch.object(
                    GatewayModel,
                    "post",
                    side_effect=[
                        {"ready": False},
                        unavailable,
                        unavailable,
                        {"ready": False},
                        unavailable,
                        unavailable,
                    ],
                ),
                patch("services.orchestrator.imagegen.asyncio.sleep", AsyncMock()),
            ):
                for stream in (False, True):
                    response = client.post(
                        "/v1/chat/completions",
                        json={
                            "stream": stream,
                            "ui_locale": "en",
                            "lang": "en",
                            "messages": [
                                {"role": "user", "content": "Generate an image"}
                            ],
                        },
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("503", response.text)
                    self.assertIn("0", response.text)
                    if stream:
                        self.assertIn("data: [DONE]", response.text)
                    self.assertNotIn('"error"', response.text)

    async def test_image_bytes_never_enter_journal(self) -> None:
        item = Interaction(reponse=f"![Image]({png()})", task_type="imagegen")
        with tempfile.TemporaryDirectory() as directory:
            write_interaction(item, Path(directory))
            text = next(Path(directory).glob("*.jsonl")).read_text()
        self.assertNotIn("data:image", text)
        self.assertNotIn(png().split(",", 1)[1], text)
        self.assertIn("[IMAGE]", text)

    async def test_gateway_large_image_keeps_text_limit(self) -> None:
        import json

        body = json.dumps({"image": "x" * 900000}).encode()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                result = await GatewayModel.post(url + "/images/generate", {}, 5)
                self.assertEqual(len(str(result["image"])), 900000)
                with self.assertRaisesRegex(ValueError, "gateway_response_limit"):
                    await GatewayModel.post(url + "/agent/complete", {}, 5)
            finally:
                server.shutdown()
                thread.join()
