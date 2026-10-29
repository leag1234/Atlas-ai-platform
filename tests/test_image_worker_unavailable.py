"""An unreachable image worker is reported as unavailable, not as a provider failure."""

import unittest
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch

import aiohttp
import imagegen
from packages.ports import atlas_ports
from services.orchestrator.image_errors import failure_message


class WorkerUnavailableTests(unittest.IsolatedAsyncioTestCase):
    async def pixels(self, address: str) -> None:
        await imagegen.pixels(
            address, "Subjects: a cube", None, 5, Decimal("1"), Decimal("0.01")
        )

    async def test_connection_refused_is_image_unavailable(self) -> None:
        # Nothing listens on this loopback address and the worker port.
        with self.assertRaises(imagegen.ImageWorkerFailure) as caught:
            await self.pixels("127.0.0.9")
        self.assertEqual(
            (caught.exception.code, caught.exception.status), ("image_unavailable", 503)
        )
        self.assertEqual(caught.exception.reserved, Decimal("0.01"))

    async def test_connect_timeout_is_image_unavailable(self) -> None:
        with patch.object(
            aiohttp.ClientSession, "post", side_effect=aiohttp.ServerTimeoutError()
        ):
            with self.assertRaises(imagegen.ImageWorkerFailure) as caught:
                await self.pixels("127.0.0.9")
        self.assertEqual(caught.exception.code, "image_unavailable")

    async def test_worker_http_500_stays_provider_error(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(500)
                self.end_headers()

        with ThreadingHTTPServer(
            ("127.0.0.8", atlas_ports().port("terminal")), Handler
        ) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaises(imagegen.ImageWorkerFailure) as caught:
                    await self.pixels("127.0.0.8")
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(
            (caught.exception.code, caught.exception.status), ("provider_error", 500)
        )

    def test_user_message_says_unavailable_not_provider_failure(self) -> None:
        message = failure_message("fr", "image_unavailable", 503, {})
        self.assertTrue(message.startswith("Le service d’image est indisponible"))
        self.assertNotIn("fournisseur", message)


if __name__ == "__main__":
    unittest.main()
