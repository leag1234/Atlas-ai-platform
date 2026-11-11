"""Real-time dictation relay: flag, authentication, strict privacy, protocol and guards."""

import asyncio
import json
import os
import tempfile
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import realtime_transcription
from services.orchestrator import dictation, dictation_cleanup
from services.orchestrator.chat_api import app
from services.orchestrator.daily_budget import DailyLedger
from services.orchestrator.terminal_stack import dictation_upgrade
from services.retrieval.project_identity import ProjectError
from services.stt import streaming

ORIGIN = {"origin": "http://testserver"}


async def signed_in(token: str) -> str:
    if token != "good":
        raise ProjectError(401, "unauthenticated")
    return "user-1"


class FakeProvider:
    """Stands for realtime_transcription.ProviderStream: scripted provider events."""

    instances: list["FakeProvider"] = []
    script: list[realtime_transcription.ProviderEvent] = []

    def __init__(self) -> None:
        self.sent_bytes = 0
        self.finished = asyncio.Event()
        self.closed = False
        self.options: dict[str, object] = {}
        FakeProvider.instances.append(self)

    async def connect(self, **options: object) -> None:
        self.options = options

    async def send(self, pcm: bytes) -> None:
        self.sent_bytes += len(pcm)

    async def finish(self) -> None:
        self.finished.set()

    async def events(self) -> AsyncIterator[realtime_transcription.ProviderEvent]:
        for event in FakeProvider.script:
            if event.kind == "done":
                await self.finished.wait()
            yield event

    async def aclose(self) -> None:
        self.closed = True


def messages(socket: Any) -> list[dict[str, Any]]:
    received: list[dict[str, Any]] = []
    while True:
        try:
            data = json.loads(socket.receive_text())
        except WebSocketDisconnect:
            return received
        received.append(data)
        if data["type"] in {"done", "error"}:
            return received


class DictationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(
            os.environ,
            {
                "ATLAS_REALTIME_DICTATION": "1",
                "ATLAS_DICTATION_ENGINES": "cloud,local",
                "ATLAS_DATA_DIR": self.directory.name,
                "ATLAS_USER_DAILY_EUR": "5",
                "MISTRAL_API_KEY": "test-key",
            },
        )
        self.environment.start()
        os.environ.pop("ATLAS_PROJECT_ORIGIN", None)
        os.environ.pop("ATLAS_DICTATION_CLEANUP", None)
        self.client = TestClient(app)
        FakeProvider.instances = []

    def tearDown(self) -> None:
        self.client.close()
        self.environment.stop()
        self.directory.cleanup()
        dictation._active.clear()

    def test_flag_off_hides_page_and_socket(self) -> None:
        with patch.dict(os.environ, {"ATLAS_REALTIME_DICTATION": "0"}):
            self.assertEqual(self.client.get("/api/atlas/dictation/").status_code, 404)
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect(
                    "/api/atlas/dictation/stream", headers=ORIGIN
                ) as socket:
                    socket.receive_text()

    def test_socket_requires_same_origin_and_a_signed_in_user(self) -> None:
        with patch("services.orchestrator.dictation.token_subject", signed_in):
            for headers, token in (
                ({"origin": "http://elsewhere"}, "good"),
                (ORIGIN, "bad"),
            ):
                self.client.cookies.set("token", token)
                with self.subTest(headers=headers, token=token):
                    with self.assertRaises(WebSocketDisconnect):
                        with self.client.websocket_connect(
                            "/api/atlas/dictation/stream", headers=headers
                        ) as socket:
                            socket.receive_text()

    def start(self, **fields: object) -> list[dict[str, Any]]:
        self.client.cookies.set("token", "good")
        with (
            patch("services.orchestrator.dictation.token_subject", signed_in),
            patch.object(realtime_transcription, "ProviderStream", FakeProvider),
        ):
            with self.client.websocket_connect(
                "/api/atlas/dictation/stream", headers=ORIGIN
            ) as socket:
                socket.send_text(json.dumps({"type": "start"} | fields))
                first = json.loads(socket.receive_text())
                if first["type"] != "ready":
                    return [first]
                socket.send_bytes(b"\0\0" * 1600)
                socket.send_text(json.dumps({"type": "stop"}))
                return [first] + messages(socket)

    def test_strict_privacy_refuses_cloud_audio(self) -> None:
        result = self.start(engine="cloud", privacy="strict", language="fr")
        self.assertEqual(result[0]["code"], "strict_privacy_unavailable")
        self.assertIn("no audio leaves Atlas", result[0]["message"])
        self.assertEqual(FakeProvider.instances, [])

    def test_cloud_session_relays_partial_final_and_meters(self) -> None:
        P = realtime_transcription.ProviderEvent
        FakeProvider.script = [
            P("turn_start", time_s=0.5),
            P("delta", "Bonjour"),
            P("delta", " Atlas."),
            P("turn_end", time_s=1.5),
            P("done", "Bonjour Atlas.", seconds=60.0),
        ]
        result = self.start(engine="cloud", language="fr")
        kinds = [message["type"] for message in result]
        self.assertEqual(kinds[0], "ready")
        self.assertIn({"type": "partial", "text": "Bonjour Atlas."}, result)
        self.assertIn({"type": "final", "index": 0, "text": "Bonjour Atlas."}, result)
        done = result[-1]
        self.assertEqual(done["type"], "done")
        # Provider seconds are billed when larger than the streamed audio.
        self.assertEqual(done["cost_eur"], "0.01")
        self.assertTrue(FakeProvider.instances[0].closed)
        self.assertEqual(FakeProvider.instances[0].options["turn_detection"], True)
        ledger = DailyLedger(Path(self.directory.name) / "daily-budget.sqlite")
        with ledger.connection() as db:
            measured, reserved = db.execute(
                "SELECT measured_micro, reserved_micro FROM budget_ledger WHERE kind='day' AND user='user-1'"
            ).fetchone()
        self.assertEqual((measured, reserved), (10000, 0))

    def test_provider_quota_is_reported_as_a_limit(self) -> None:
        class Limited(FakeProvider):
            async def connect(self, **options: object) -> None:
                raise realtime_transcription.ProviderLimit(429)

        self.client.cookies.set("token", "good")
        with (
            patch("services.orchestrator.dictation.token_subject", signed_in),
            patch.object(realtime_transcription, "ProviderStream", Limited),
        ):
            with self.client.websocket_connect(
                "/api/atlas/dictation/stream", headers=ORIGIN
            ) as socket:
                socket.send_text(json.dumps({"type": "start", "engine": "cloud"}))
                error = json.loads(socket.receive_text())
        self.assertEqual(error["code"], "provider_limit")
        self.assertEqual(error["provider_status"], 429)

    def test_strict_privacy_keeps_cleanup_off_without_an_owned_model(self) -> None:
        os.environ.pop("ATLAS_OWNED_VLLM_BASE_URL", None)
        with patch.dict(os.environ, {"ATLAS_DICTATION_CLEANUP": "1"}):
            session = dictation.Session(
                object(),  # type: ignore[arg-type]
                "user-1",
                {"engine": "local", "privacy": "strict", "cleanup": True},
                dictation.Limits.current(),
            )
            self.assertIsNone(session.cleaner)
            session = dictation.Session(
                object(),  # type: ignore[arg-type]
                "user-1",
                {"engine": "cloud", "cleanup": True},
                dictation.Limits.current(),
            )
            self.assertIsNotNone(session.cleaner)

    def test_one_session_per_user(self) -> None:
        dictation._active.add("user-1")
        result = self.start(engine="cloud")
        self.assertEqual(result[0]["code"], "busy")

    def test_frames_are_bounded(self) -> None:
        FakeProvider.script = [realtime_transcription.ProviderEvent("done")]
        self.client.cookies.set("token", "good")
        with (
            patch("services.orchestrator.dictation.token_subject", signed_in),
            patch.object(realtime_transcription, "ProviderStream", FakeProvider),
        ):
            with self.client.websocket_connect(
                "/api/atlas/dictation/stream", headers=ORIGIN
            ) as socket:
                socket.send_text(json.dumps({"type": "start", "engine": "cloud"}))
                self.assertEqual(json.loads(socket.receive_text())["type"], "ready")
                socket.send_bytes(b"\0" * 20001)
                error = messages(socket)[-1]
        self.assertEqual(error["code"], "invalid_audio")
        self.assertTrue(FakeProvider.instances[0].closed)
        self.assertNotIn("user-1", dictation._active)


class ProviderTests(unittest.TestCase):
    def test_events_are_normalised_without_provider_messages(self) -> None:
        normalise = realtime_transcription.normalise
        self.assertEqual(
            normalise({"type": "transcription.text.delta", "text": " mot"}),
            realtime_transcription.ProviderEvent("delta", " mot"),
        )
        done = normalise(
            {
                "type": "transcription.done",
                "text": "t",
                "usage": {"prompt_audio_seconds": 7},
            }
        )
        assert done is not None
        self.assertEqual(done.seconds, 7.0)
        error = normalise(
            {"type": "error", "error": {"message": "quoted input", "code": 3051}}
        )
        assert error is not None
        self.assertEqual((error.kind, error.code, error.text), ("error", "3051", ""))
        self.assertIsNone(normalise({"type": "session.created"}))

    def test_eu_socket_and_conservative_price(self) -> None:
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "k"}):
            os.environ.pop("ATLAS_REALTIME_TRANSCRIPTION_PROVIDER", None)
            self.assertEqual(
                realtime_transcription.socket_url(),
                "wss://api.eu.mistral.ai/v1/audio/transcriptions/realtime",
            )
            self.assertTrue(realtime_transcription.configured())
        self.assertEqual(realtime_transcription.price_micro(60), 10000)
        self.assertEqual(realtime_transcription.price_micro(0.1), 17)

    def test_delay_must_be_a_documented_value(self) -> None:
        stream = realtime_transcription.ProviderStream()
        with self.assertRaises(ValueError):
            asyncio.run(
                stream.connect(language="fr", delay_ms=500, turn_detection=False)
            )


class CloudSegmentTests(unittest.TestCase):
    def test_turn_text_becomes_final_after_the_streaming_delay(self) -> None:
        P = realtime_transcription.ProviderEvent

        class Slow:
            sent_bytes = 0

            async def events(
                self,
            ) -> AsyncIterator[realtime_transcription.ProviderEvent]:
                yield P("delta", "Un")
                yield P("turn_end", time_s=1.0)
                yield P("delta", " deux.")
                await asyncio.sleep(0.4)
                yield P("turn_start", time_s=3.0)
                yield P("delta", "Trois")
                yield P("done", seconds=4.0)

        async def collect() -> list[tuple[str, str]]:
            segments = dictation.CloudSegments(Slow(), 80)
            segments.settle = 0.2
            return [(e.kind, e.text) async for e in segments.events()]

        result = asyncio.run(collect())
        self.assertIn(("final", "Un deux."), result)
        self.assertEqual(result[-2:], [("final", "Trois"), ("done", "")])


class LocalStreamTests(unittest.TestCase):
    def test_vad_segments_give_partials_and_one_final_per_utterance(self) -> None:
        decoded: list[int] = []

        def decode(samples: Any) -> str:
            decoded.append(len(samples))
            return "texte"

        async def run() -> list[str]:
            stream = streaming.LocalStream(decode)
            stream.vad = lambda frame: 1.0 if float(np.abs(frame).max()) > 0.1 else 0.0
            tone = (np.ones(16000 * 2) * 0.5 * 32767).astype("<i2").tobytes()
            silence = bytes(32000)
            for chunk in (silence, tone, silence, silence):
                for start in range(0, len(chunk), 3200):
                    await stream.send(chunk[start : start + 3200])
                    await asyncio.sleep(0.01)
            await stream.finish()
            return [event.kind async for event in stream.events()]

        with patch.object(streaming, "PARTIAL_INTERVAL", 0.0):
            kinds = asyncio.run(run())
        self.assertEqual(kinds.count("final"), 1)
        self.assertEqual(kinds[0], "turn_start")
        self.assertEqual(kinds[-1], "done")
        self.assertIn("turn_end", kinds)


class CleanupGuardTests(unittest.TestCase):
    def test_guard_accepts_punctuation_and_casing(self) -> None:
        self.assertTrue(
            dictation_cleanup.accepted(
                "bonjour je voudrais une table pour quatre personnes",
                "Bonjour, je voudrais une table pour quatre personnes.",
            )
        )

    def test_guard_rejects_inserted_or_rephrased_words(self) -> None:
        self.assertFalse(
            dictation_cleanup.accepted(
                "envoie le rapport", "Envoie le rapport complet."
            )
        )
        self.assertFalse(
            dictation_cleanup.accepted(
                "envoie le rapport avant vendredi",
                "Peux-tu transmettre le document ?",
            )
        )

    def test_edit_counts(self) -> None:
        change = dictation_cleanup.edits(["a", "b", "c"], ["a", "x", "c", "d"])
        self.assertEqual(
            (change.substitutions, change.deletions, change.insertions), (1, 0, 1)
        )


class ProxyTests(unittest.TestCase):
    def test_only_the_dictation_socket_keeps_its_upgrade(self) -> None:
        upgrade = b"GET /api/atlas/dictation/stream HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade"
        self.assertTrue(dictation_upgrade("/api/atlas/dictation/stream", upgrade))
        self.assertFalse(dictation_upgrade("/api/atlas/voice/speech", upgrade))
        self.assertFalse(
            dictation_upgrade(
                "/api/atlas/dictation/stream",
                b"GET /api/atlas/dictation/stream HTTP/1.1\r\nConnection: close",
            )
        )


if __name__ == "__main__":
    unittest.main()
