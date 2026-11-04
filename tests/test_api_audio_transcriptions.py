"""OpenAI-format speech to text on the key API, with a fake gateway provider (no network)."""

import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import transcription
from services.orchestrator.api_keys import Store
from services.orchestrator.chat_api import app

URL = "/api/atlas/v1/audio/transcriptions"
AUDIO = b"RIFF-fictional-audio-\x00\xff-bytes"
SPOKEN = "fictional spoken sentence"


class TranscriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(
            root / "keys.sqlite", "fictional-pepper-" * 4, lambda: 1800000000.0
        )
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ATLAS_DATA_DIR": str(root),
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_PROJECT_DSN": "",
                    "ATLAS_USER_DAILY_EUR": "10",
                    "ATLAS_GLOBAL_DAILY_EUR": "20",
                },
            )
        )
        self.enterContext(
            patch(
                "services.orchestrator.api_key_edge.configured_store",
                return_value=self.store,
            )
        )
        self.calls: list[dict[str, Any]] = []
        self.failure: Exception | None = None

        async def fake_transcribe(audio: bytes, **options: Any) -> dict[str, Any]:
            self.calls.append({"audio": audio, **options})
            if self.failure is not None:
                raise self.failure
            return {
                "text": SPOKEN,
                "language": "fr",
                "duration": 3.0,
                "cost_micro": transcription.price_micro(3.0),
                **(
                    {"segments": [{"id": 0, "start": 0.0, "end": 3.0, "text": SPOKEN}]}
                    if options["segments"]
                    else {}
                ),
            }

        self.enterContext(patch("transcription.transcribe", fake_transcribe))
        self.enterContext(patch("transcription.configured", return_value=True))
        self.client = self.enterContext(TestClient(app))
        self.row, self.headers = self.key(["model"])

    def key(
        self, scopes: list[str], daily_micro_eur: int = 1000000
    ) -> tuple[dict[str, Any], dict[str, str]]:
        row, secret = self.store.create(
            org_id="fictional-org",
            member_id="fictional-member",
            subject="tester@example.invalid",
            budget_user="fictional-user",
            name="transcription",
            scopes=scopes,
            created_by="operator",
            daily_requests=100,
            daily_micro_eur=daily_micro_eur,
        )
        return row, {"Authorization": "Bearer " + secret}

    def post(
        self,
        fields: dict[str, Any] | list[tuple[str, str]] | None = None,
        audio: bytes | None = AUDIO,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if isinstance(fields, list):
            grouped: dict[str, Any] = {}
            for name, value in fields:
                grouped.setdefault(name, []).append(value)
            fields = grouped
        return self.client.post(
            URL,
            headers=headers or self.headers,
            data=fields if fields is not None else {"model": "whisper-1"},
            files={"file": ("speech.wav", audio, "audio/wav")}
            if audio is not None
            else {"response_format": (None, "json")},
        )

    def test_json_default_with_accounting(self) -> None:
        reply = self.post({"model": "whisper-1", "language": "fr"})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(
            reply.json(),
            {
                "text": SPOKEN,
                "language": "fr",
                "duration": 3.0,
                "usage": {"type": "duration", "seconds": 3.0},
            },
        )
        call = self.calls[-1]
        self.assertEqual(call["audio"], AUDIO)
        self.assertEqual(call["filename"], "speech.wav")
        self.assertEqual(call["content_type"], "audio/wav")
        self.assertEqual(call["language"], "fr")
        self.assertIsNone(call["temperature"])
        self.assertFalse(call["segments"])
        self.assertEqual(
            self.store.usage(self.row["id"])["charged_micro_eur"],
            transcription.price_micro(3.0),
        )
        self.assertEqual(transcription.price_micro(3.0), 200)

    def test_text_and_verbose_json(self) -> None:
        reply = self.post({"model": "any", "response_format": "text"})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.text, SPOKEN)
        self.assertTrue(reply.headers["content-type"].startswith("text/plain"))
        reply = self.post(
            [
                ("model", "any"),
                ("response_format", "verbose_json"),
                ("temperature", "0.2"),
                ("timestamp_granularities[]", "segment"),
            ]
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        body = reply.json()
        self.assertEqual(body["task"], "transcribe")
        self.assertEqual(body["text"], SPOKEN)
        self.assertEqual(body["segments"][0]["end"], 3.0)
        self.assertTrue(self.calls[-1]["segments"])
        self.assertEqual(self.calls[-1]["temperature"], 0.2)

    def test_refusals_by_name(self) -> None:
        cases: list[tuple[Any, bytes | None, str]] = [
            ({"model": "any"}, None, "file: exactly one non-empty audio file"),
            ({"model": "any"}, b"", "file: exactly one non-empty audio file"),
            ({"model": "any", "prompt": "names"}, AUDIO, "prompt is not supported"),
            (
                {"model": "any", "language": "french"},
                AUDIO,
                "language: an ISO-639-1 code such as fr",
            ),
            (
                {"model": "any", "response_format": "srt"},
                AUDIO,
                "response_format: json, text or verbose_json (srt and vtt are not "
                "supported)",
            ),
            ({"model": "any", "temperature": "2"}, AUDIO, "temperature: 0 to 1"),
            (
                [
                    ("response_format", "verbose_json"),
                    ("timestamp_granularities[]", "word"),
                ],
                AUDIO,
                "timestamp_granularities: word is not supported, only segment",
            ),
            (
                {"timestamp_granularities[]": "segment"},
                AUDIO,
                "timestamp_granularities: segment, with response_format verbose_json",
            ),
            (
                {"model": "any", "stream": "true"},
                AUDIO,
                "unsupported field: stream",
            ),
        ]
        for fields, audio, expected in cases:
            with self.subTest(expected=expected):
                reply = self.post(fields, audio)
                self.assertEqual(reply.status_code, 400, reply.text)
                self.assertEqual(reply.json()["error"]["code"], "invalid_request")
                self.assertEqual(reply.json()["error"]["message"], expected)
        reply = self.client.post(
            URL, headers=self.headers, json={"model": "any", "file": "x"}
        )
        self.assertEqual(reply.status_code, 400, reply.text)
        self.assertIn("multipart/form-data", reply.json()["error"]["message"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.usage(self.row["id"])["charged_micro_eur"], 0)

    def test_size_limit(self) -> None:
        self.enterContext(patch("transcription.MAX_FILE_BYTES", 1000))
        reply = self.post(audio=b"x" * 1001)
        self.assertEqual(reply.status_code, 413, reply.text)
        error = reply.json()["error"]
        self.assertEqual(
            (error["code"], error["measured"], error["limit"]),
            ("body_limit", 1001, 1000),
        )
        reply = self.post(audio=b"x" * 200000)
        self.assertEqual(reply.status_code, 413, reply.text)
        self.assertEqual(reply.json()["error"]["limit"], 1000 + 64 * 1024)
        self.assertEqual(self.calls, [])

    def test_strict_privacy_and_unconfigured_are_named_503(self) -> None:
        reply = self.post(headers=self.headers | {"X-Atlas-Privacy": "strict"})
        self.assertEqual(reply.status_code, 503, reply.text)
        self.assertEqual(reply.json()["error"]["code"], "strict_privacy_unavailable")
        with patch("transcription.configured", return_value=False):
            reply = self.post()
            names = [
                model["id"]
                for model in self.client.get(
                    "/api/atlas/v1/models", headers=self.headers
                ).json()["data"]
            ]
        self.assertEqual(reply.status_code, 503, reply.text)
        self.assertEqual(reply.json()["error"]["code"], "transcription_unavailable")
        self.assertNotIn("atlas-transcribe", names)
        names = [
            model["id"]
            for model in self.client.get(
                "/api/atlas/v1/models", headers=self.headers
            ).json()["data"]
        ]
        self.assertIn("atlas-transcribe", names)
        self.assertEqual(self.calls, [])

    def test_provider_failures_are_named(self) -> None:
        cases: list[tuple[Exception, int, str]] = [
            (transcription.ProviderLimit(429), 429, "provider_limit"),
            (RuntimeError("provider_error"), 502, "provider_error"),
            (transcription.ProviderUnavailable(), 503, "transcription_unavailable"),
            (transcription.AudioRefused(), 400, "invalid_request"),
        ]
        for failure, status, code in cases:
            with self.subTest(code=code):
                self.failure = failure
                reply = self.post()
                self.assertEqual(reply.status_code, status, reply.text)
                self.assertEqual(reply.json()["error"]["code"], code)
        self.assertEqual(self.store.usage(self.row["id"])["charged_micro_eur"], 0)

    def test_quota_refusal_before_the_provider(self) -> None:
        row, headers = self.key(["model"], daily_micro_eur=1)
        reply = self.post(headers=headers)
        self.assertEqual(reply.status_code, 429, reply.text)
        self.assertEqual(reply.json()["error"]["code"], "quota_exceeded")
        self.assertEqual(self.calls, [])
        _, headers = self.key(["assistant"])
        reply = self.post(headers=headers)
        self.assertEqual(reply.status_code, 403, reply.text)

    def test_translations_refused_by_name(self) -> None:
        reply = self.client.post(
            "/api/atlas/v1/audio/translations",
            headers=self.headers,
            data={"model": "any"},
            files={"file": ("speech.wav", AUDIO, "audio/wav")},
        )
        self.assertEqual(reply.status_code, 400, reply.text)
        self.assertEqual(reply.json()["error"]["code"], "translation_not_supported")

    def test_logs_hold_neither_audio_nor_transcript(self) -> None:
        with self.assertLogs(level="DEBUG") as captured:
            reply = self.post({"model": "any", "language": "fr"})
        self.assertEqual(reply.status_code, 200, reply.text)
        text = "\n".join(record.getMessage() for record in captured.records)
        events = [
            json.loads(record.getMessage())
            for record in captured.records
            if record.name == "atlas.api"
        ]
        self.assertIn("api_transcription", [event["event"] for event in events])
        self.assertNotIn(SPOKEN, text)
        self.assertNotIn("fictional-audio", text)
        self.assertNotIn("speech.wav", text)


if __name__ == "__main__":
    unittest.main()
