"""A worker that does not report its model still returns a usable image."""

import base64
import io
import json
import unittest
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import imagegen
from PIL import Image
from packages.ports import atlas_ports


def png() -> str:
    output = io.BytesIO()
    Image.new("RGB", (1024, 1024), "white").save(output, "PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


class WorkerCompatTests(unittest.IsolatedAsyncioTestCase):
    async def generate(self, payload: dict[str, object]) -> dict[str, object]:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

        with ThreadingHTTPServer(
            ("127.0.0.7", atlas_ports().port("terminal")), Handler
        ) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                return await imagegen.pixels(
                    "127.0.0.7",
                    "Subjects: a cube",
                    None,
                    5,
                    Decimal("1"),
                    Decimal("0.01"),
                )
            finally:
                server.shutdown()
                thread.join()

    async def test_missing_model_is_reported_as_unreported(self) -> None:
        result = await self.generate(
            {"image": png(), "seed": 5, "steps": 4, "seconds": 0.1}
        )
        self.assertEqual(result["model"], "unreported")
        self.assertEqual(result["seed"], 5)

    async def test_reported_model_is_kept_and_invalid_model_rejected(self) -> None:
        base = {"image": png(), "seed": 5, "steps": 4, "seconds": 0.1}
        result = await self.generate(dict(base, model="fictional/worker-model"))
        self.assertEqual(result["model"], "fictional/worker-model")
        with self.assertRaisesRegex(ValueError, "invalid_provider_model"):
            await self.generate(dict(base, model=""))


if __name__ == "__main__":
    unittest.main()
