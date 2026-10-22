"""Limit diagnostics cross the real gateway HTTP boundary without input contents."""

import json
import unittest
from http.client import HTTPConnection
from threading import Thread
from unittest.mock import Mock, patch

from http_gateway import serve
from packages.images import VisionMessage
from packages.validation import describe_validation
from pydantic import ValidationError


class LimitTransportTests(unittest.TestCase):
    def test_gateway_money_refusal_is_not_http_payload_too_large(self) -> None:
        from packages.limits import ProviderLimitError

        async def reject(*args: object) -> object:
            raise ProviderLimitError("cost_budget", 66633, 47137, "microEUR")

        server = serve(Mock(), 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch("http_gateway.AgentProvider") as provider:
                provider.return_value.complete = reject
                client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                client.request("POST", "/agent/complete", body="{}")
                response = client.getresponse()
                self.assertEqual(response.status, 429)
                value = json.loads(response.read())
                self.assertEqual((value["measured"], value["limit"]), (66633, 47137))
                client.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_gateway_body_reports_bytes(self) -> None:
        server = serve(Mock(), 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            client.request("POST", "/embeddings", headers={"Content-Length": "800001"})
            response = client.getresponse()
            self.assertEqual(response.status, 413)
            value = json.loads(response.read())
            self.assertEqual(value["measured"], 800001)
            self.assertEqual(value["limit"], 800000)
            self.assertEqual(value["unit"], "bytes")
            client.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_multipart_limit_reports_count(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            VisionMessage.model_validate(
                {"role": "user", "content": [{"type": "text", "text": "private"}] * 17}
            )
        detail = describe_validation(caught.exception)
        self.assertIn("17", detail)
        self.assertIn("16", detail)
        self.assertNotIn("private", detail)


class MonetaryTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_monetary_limit_is_not_mislabeled_as_bytes(self) -> None:
        import asyncio
        from unittest.mock import patch
        from packages.limits import ProviderLimitError
        from services.orchestrator.model import GatewayModel, GatewayError

        async def reject(*args: object) -> object:
            raise ProviderLimitError("cost_budget", 66633, 47137, "microEUR")

        server = serve(Mock(), 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch("http_gateway.AgentProvider") as provider:
                provider.return_value.complete = reject
                with self.assertRaises(GatewayError) as caught:
                    await GatewayModel.post(
                        f"http://127.0.0.1:{server.server_port}/agent/complete", {}, 5
                    )
            self.assertEqual(caught.exception.code, "cost_budget")
            self.assertIn("66633", caught.exception.detail)
            self.assertIn("47137", caught.exception.detail)
            self.assertIn("microEUR", caught.exception.detail)
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()

    async def test_streamed_money_refusal_discloses_remaining_and_request_cap(
        self,
    ) -> None:
        from aiohttp import web
        from services.orchestrator.model import GatewayError
        from services.orchestrator.stream_client import receive

        async def endpoint(request: web.Request) -> web.Response:
            event = {
                "error": "measured_limit",
                "code": "cost_budget",
                "measured": 66633,
                "limit": 47137,
                "unit": "microEUR",
            }
            return web.Response(
                text="data: " + json.dumps(event) + "\n\n",
                content_type="text/event-stream",
            )

        async def sink(event: dict[str, object]) -> None:
            return None

        app = web.Application()
        app.router.add_post("/stream", endpoint)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            port = runner.addresses[0][1]
            with self.assertRaises(GatewayError) as caught:
                await receive(
                    f"http://127.0.0.1:{port}/stream",
                    {"profile": "atlas-qwen", "produces_files": True},
                    5,
                    sink,
                )
            self.assertEqual(caught.exception.status, 429)
            self.assertEqual(caught.exception.code, "cost_budget")
            for value in ("66633", "47137", "0.30"):
                self.assertIn(value, caught.exception.detail)
        finally:
            await runner.cleanup()
