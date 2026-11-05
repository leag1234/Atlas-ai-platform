"""Gateway transcription provider against a local fake server (no network)."""

import asyncio
import json
import os
import threading
import unittest
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from unittest.mock import patch

import transcription


class FakeProvider(BaseHTTPRequestHandler):
    status = 200
    answer: dict[str, Any] = {}
    received: list[dict[str, Any]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        header = Message()
        header["content-type"] = self.headers["content-type"]
        boundary = str(header.get_param("boundary")).encode()
        fields: dict[str, bytes] = {}
        for section in body.split(b"--" + boundary)[1:-1]:
            head, _, data = section[2:-2].partition(b"\r\n\r\n")
            part = Message()
            for line in head.decode().split("\r\n"):
                name, _, value = line.partition(":")
                part[name] = value.strip()
            fields[str(part.get_param("name", header="content-disposition"))] = data
        FakeProvider.received.append(
            {
                "path": self.path,
                "authorization": self.headers.get("authorization"),
                "fields": fields,
            }
        )
        payload = json.dumps(FakeProvider.answer).encode()
        self.send_response(FakeProvider.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_: object) -> None:
        pass


def run(**options: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "filename": "speech.wav",
        "content_type": "audio/wav",
        "language": None,
        "temperature": None,
        "segments": False,
        "timeout": 10,
    }
    values.update(options)
    return asyncio.run(transcription.transcribe(b"fictional-audio", **values))


class GatewayTranscriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        server = HTTPServer(("127.0.0.1", 0), FakeProvider)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            server.shutdown()
            server.server_close()
            thread.join()

        self.addCleanup(stop)
        url = f"http://127.0.0.1:{server.server_port}/v1"
        self.enterContext(patch("transcription.base_url", return_value=url))
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ATLAS_TRANSCRIPTION_PROVIDER": "mistral",
                    "MISTRAL_API_KEY": "fictional-key",
                },
            )
        )
        FakeProvider.status = 200
        FakeProvider.received = []
        FakeProvider.answer = {
            "text": "fictional sentence",
            "language": "fr",
            "segments": [{"text": "fictional sentence", "start": 0.0, "end": 2.5}],
            "usage": {"prompt_audio_seconds": 3},
        }

    def test_form_fields_and_result(self) -> None:
        result = run(language="fr", temperature=0.0, segments=True)
        self.assertEqual(result["text"], "fictional sentence")
        self.assertEqual(result["duration"], 3.0)
        self.assertEqual(result["cost_micro"], 200)
        self.assertEqual(result["segments"][0]["end"], 2.5)
        sent = FakeProvider.received[-1]
        self.assertEqual(sent["path"], "/v1/audio/transcriptions")
        self.assertEqual(sent["authorization"], "Bearer fictional-key")
        self.assertEqual(sent["fields"]["file"], b"fictional-audio")
        self.assertEqual(sent["fields"]["model"], b"voxtral-mini-latest")
        self.assertEqual(sent["fields"]["language"], b"fr")
        self.assertEqual(sent["fields"]["timestamp_granularities"], b"segment")
        result = run()
        self.assertNotIn("segments", result)
        self.assertNotIn("language", FakeProvider.received[-1]["fields"])

    def test_statuses_are_named(self) -> None:
        cases: list[tuple[int, type[Exception]]] = [
            (429, transcription.ProviderLimit),
            (422, transcription.AudioRefused),
            (500, RuntimeError),
            (503, RuntimeError),
            (401, RuntimeError),
        ]
        for status, failure in cases:
            with self.subTest(status=status):
                FakeProvider.status = status
                with self.assertRaises(failure):
                    run()
        FakeProvider.status = 200
        FakeProvider.answer = {"language": "fr"}
        with self.assertRaises(RuntimeError):
            run()

    def test_unreachable_provider_and_configuration(self) -> None:
        with patch("transcription.base_url", return_value="http://127.0.0.1:1/v1"):
            with self.assertRaises(transcription.ProviderUnavailable):
                run()
        self.assertTrue(transcription.configured())
        with patch.dict(os.environ, {"MISTRAL_API_KEY": ""}):
            self.assertFalse(transcription.configured())
        with patch.dict(os.environ, {"ATLAS_TRANSCRIPTION_PROVIDER": "ionos"}):
            self.assertFalse(transcription.configured())

    def test_prices(self) -> None:
        self.assertEqual(transcription.price_micro(60), 4000)
        self.assertEqual(transcription.estimated_micro(100), 4000)
        self.assertEqual(transcription.estimated_micro(16000 * 120), 8000)


if __name__ == "__main__":
    unittest.main()
