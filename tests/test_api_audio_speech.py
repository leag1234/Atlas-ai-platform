"""API key edge speech synthesis: local service only, with explicit failures."""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
import tempfile
from unittest.mock import patch

from fastapi.testclient import TestClient

from services.orchestrator.api_key_edge import TTS_MICRO_EUR_PER_CHARACTER
from services.orchestrator.api_keys import Store
from services.orchestrator.chat_api import app


class FakeTTSHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.server.payload = payload  # type: ignore[attr-defined]
        audio = b"fake-audio"
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("X-Atlas-TTS-Voice", "en_US-amy-medium")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)

    def log_message(self, *_: object) -> None:
        pass


class Server:
    def __init__(self) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), FakeTTSHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "Server":
        return self

    def __exit__(self, *_: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class SpeechTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = [1800000000.0]
        self.store = Store(
            self.root / "keys.sqlite", "test-pepper-" * 4, lambda: self.now[0]
        )
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ATLAS_API_KEYS_DB": str(self.store.path),
                    "ATLAS_API_KEY_PEPPER": "test-pepper-" * 4,
                    "ATLAS_API_KEY_ADMIN_TOKEN": "test-admin-" * 4,
                    "ATLAS_DATA_DIR": str(self.root),
                    "ATLAS_INTERACTION_DIR": str(self.root / "chat-log"),
                    "ATLAS_USER_DAILY_EUR": "10",
                    "ATLAS_GLOBAL_DAILY_EUR": "20",
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_PROJECT_DSN": "",
                },
            )
        )
        self.enterContext(
            patch(
                "services.orchestrator.api_key_edge.configured_store",
                return_value=self.store,
            )
        )
        self.row, self.key = self.store.create(
            org_id="test-org",
            member_id="test-member",
            subject="tester@example.invalid",
            budget_user="webui-test-id",
            name="speech",
            scopes=["model"],
            created_by="operator",
        )
        self.headers = {"Authorization": "Bearer " + self.key}
        self.client = self.enterContext(TestClient(app))

    def request(self, **body: Any) -> Any:
        return self.client.post(
            "/api/atlas/v1/audio/speech", headers=self.headers, json=body
        )

    def test_success_mp3_wav_voice_and_accounting(self) -> None:
        with Server() as server, patch.dict(os.environ, {"ATLAS_TTS_URL": server.url}):
            for response_format, media_type in (
                ("mp3", "audio/mpeg"),
                ("wav", "audio/wav"),
            ):
                reply = self.request(
                    model="any-name",
                    input="Hello",
                    voice="ignored",
                    response_format=response_format,
                )
                self.assertEqual(reply.status_code, 200, reply.text)
                self.assertEqual(reply.content, b"fake-audio")
                self.assertEqual(reply.headers["content-type"], media_type)
                self.assertEqual(reply.headers["x-atlas-tts-voice"], "en_US-amy-medium")
            payload = server.server.payload  # type: ignore[attr-defined]
            self.assertEqual(payload["model"], "atlas-tts")
            self.assertEqual(payload["input"], "Hello")
            self.assertEqual(
                self.store.usage(self.row["id"])["charged_micro_eur"],
                2 * len("Hello") * TTS_MICRO_EUR_PER_CHARACTER,
            )

    def test_validation_refusals(self) -> None:
        cases: list[tuple[dict[str, Any], str]] = [
            ({"model": "any", "input": ""}, "input: a non-empty string"),
            (
                {"model": "any", "input": "x" * 12001},
                "input_limit",
            ),
            (
                {"model": "any", "input": "Hello", "response_format": "opus"},
                "response_format: mp3 or wav",
            ),
            (
                {"model": "any", "input": "Hello", "speed": 1.1},
                "speed: only 1.0 is supported",
            ),
            (
                {"model": "any", "input": "Hello", "instructions": "be brief"},
                "instructions are not supported",
            ),
            (
                {"model": "any", "input": "Hello", "unknown": True},
                "unsupported field: unknown",
            ),
        ]
        for body, expected in cases:
            with self.subTest(expected=expected):
                reply = self.request(**body)
                self.assertEqual(reply.status_code, 400, reply.text)
                payload = reply.json()["error"]
                if expected == "input_limit":
                    self.assertEqual(payload["code"], "input_limit")
                    self.assertEqual(payload["measured"], 12001)
                    self.assertEqual(payload["limit"], 12000)
                    self.assertEqual(payload["unit"], "characters")
                else:
                    self.assertEqual(payload["code"], "invalid_request")
                    self.assertEqual(payload["message"], expected)

    def test_budget_refusal(self) -> None:
        row, key = self.store.create(
            org_id="test-org",
            member_id="test-member",
            subject="budget@example.invalid",
            budget_user="budget-test-id",
            name="budget",
            scopes=["model"],
            created_by="operator",
            daily_micro_eur=1,
        )
        headers = {"Authorization": "Bearer " + key}
        self.enterContext(
            patch.dict(os.environ, {"ATLAS_TTS_URL": "http://127.0.0.1:1"})
        )
        reply = self.client.post(
            "/api/atlas/v1/audio/speech",
            headers=headers,
            json={"model": "any", "input": "Hello"},
        )
        self.assertEqual(reply.status_code, 429, reply.text)
        self.assertEqual(reply.json()["error"]["code"], "quota_exceeded")
        self.assertEqual(self.store.usage(row["id"])["charged_micro_eur"], 0)

    def test_unreachable_service_is_named_503(self) -> None:
        self.enterContext(
            patch.dict(os.environ, {"ATLAS_TTS_URL": "http://127.0.0.1:1"})
        )
        reply = self.request(model="any", input="Hello")
        self.assertEqual(reply.status_code, 503, reply.text)
        self.assertEqual(reply.json()["error"]["code"], "tts_unavailable")
        self.assertEqual(self.store.usage(self.row["id"])["charged_micro_eur"], 0)

    def test_not_enabled_is_named_503(self) -> None:
        self.enterContext(patch.dict(os.environ, {"ATLAS_TTS_URL": ""}))
        reply = self.request(model="any", input="Hello")
        self.assertEqual(reply.status_code, 503, reply.text)
        self.assertEqual(reply.json()["error"]["code"], "tts_unavailable")
        names = [
            model["id"]
            for model in self.client.get(
                "/api/atlas/v1/models", headers=self.headers
            ).json()["data"]
        ]
        self.assertNotIn("atlas-tts", names)

    def test_service_refusing_the_text_is_a_400_not_an_outage(self) -> None:
        class Refusing(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("content-length", "0")))
                self.send_response(400)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Refusing)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(
                os.environ, {"ATLAS_TTS_URL": f"http://127.0.0.1:{server.server_port}"}
            ):
                reply = self.request(model="any", input="...")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(reply.status_code, 400, reply.text)
        self.assertEqual(reply.json()["error"]["code"], "invalid_request")
        self.assertEqual(self.store.usage(self.row["id"])["charged_micro_eur"], 0)

    def test_log_omits_text_and_strict_privacy_works(self) -> None:
        with Server() as server, patch.dict(os.environ, {"ATLAS_TTS_URL": server.url}):
            self.assert_speech_log_and_privacy()

    def assert_speech_log_and_privacy(self) -> None:
        with self.assertLogs("atlas.api", level="INFO") as captured:
            reply = self.client.post(
                "/api/atlas/v1/audio/speech",
                headers=self.headers | {"X-Atlas-Privacy": "strict"},
                json={"model": "any", "input": "secret sentence"},
            )
        records = captured.records
        self.assertEqual(reply.status_code, 200, reply.text)
        events = [json.loads(record.getMessage()) for record in records]
        self.assertIn("api_speech", [event.get("event") for event in events])
        self.assertNotIn("secret sentence", reply.text)
        self.assertNotIn("secret sentence", str(events))


if __name__ == "__main__":
    unittest.main()
