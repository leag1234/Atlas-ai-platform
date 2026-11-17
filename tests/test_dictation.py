"""Real-time dictation relay: flag, authentication, strict privacy, protocol and guards."""

import asyncio
import json
import os
import re
import tempfile
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import numpy as np
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import realtime_transcription
import transcription
from services.orchestrator import dictation, dictation_cleanup, dictation_text
from services.orchestrator.chat_api import app
from services.orchestrator.daily_budget import DailyLedger
from services.orchestrator.terminal_stack import dictation_upgrade
from services.retrieval.project_identity import ProjectError
from services.stt import streaming

ORIGIN = {"origin": "http://testserver"}


async def signed_in(token: str) -> dict[str, str]:
    if token != "good":
        raise ProjectError(401, "unauthenticated")
    return {"id": "user-1", "email": "member@example.test"}


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
                # Tests that exercise the final pass turn it on with a fake provider.
                "ATLAS_DICTATION_FINAL_PASS": "off",
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
        dictation._local.clear()

    def test_flag_off_hides_page_and_socket(self) -> None:
        with patch.dict(os.environ, {"ATLAS_REALTIME_DICTATION": "0"}):
            self.assertEqual(self.client.get("/api/atlas/dictation/").status_code, 404)
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect(
                    "/api/atlas/dictation/stream", headers=ORIGIN
                ) as socket:
                    socket.receive_text()

    def test_socket_requires_same_origin_and_a_signed_in_user(self) -> None:
        with patch("services.orchestrator.dictation.token_user", signed_in):
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

    def start(
        self, audio: bytes = b"\0\0" * 1600, **fields: object
    ) -> list[dict[str, Any]]:
        self.client.cookies.set("token", "good")
        with (
            patch("services.orchestrator.dictation.token_user", signed_in),
            patch.object(realtime_transcription, "ProviderStream", FakeProvider),
        ):
            with self.client.websocket_connect(
                "/api/atlas/dictation/stream", headers=ORIGIN
            ) as socket:
                socket.send_text(json.dumps({"type": "start"} | fields))
                first = json.loads(socket.receive_text())
                if first["type"] != "ready":
                    return [first]
                for offset in range(0, len(audio), 16000):
                    socket.send_bytes(audio[offset : offset + 16000])
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
            patch("services.orchestrator.dictation.token_user", signed_in),
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
                {"cleanup": True},
                dictation.Limits.current(),
                engine="local",
                privacy="strict",
            )
            self.assertIsNone(session.cleaner)
            session = dictation.Session(
                object(),  # type: ignore[arg-type]
                "user-1",
                {"cleanup": True},
                dictation.Limits.current(),
                engine="cloud",
                privacy="standard",
            )
            self.assertIsNotNone(session.cleaner)

    def test_one_session_per_user(self) -> None:
        dictation._active.add("user-1")
        result = self.start(engine="cloud")
        self.assertEqual(result[0]["code"], "session_open")

    def test_frames_are_bounded(self) -> None:
        FakeProvider.script = [realtime_transcription.ProviderEvent("done")]
        self.client.cookies.set("token", "good")
        with (
            patch("services.orchestrator.dictation.token_user", signed_in),
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

    def test_busy_local_engine_falls_back_to_cloud_outside_strict_privacy(self) -> None:
        P = realtime_transcription.ProviderEvent
        FakeProvider.script = [P("done", seconds=0.1)]
        dictation._local.update({"other-1", "other-2"})  # the local cap (2) is reached
        with patch.object(dictation, "local_installed", lambda: True):
            result = self.start(language="fr")
        self.assertEqual(result[0]["type"], "ready")
        self.assertEqual((result[0]["engine"], result[0]["fallback"]), ("cloud", True))
        self.assertEqual(result[0]["privacy"], "standard")
        self.assertEqual(len(FakeProvider.instances), 1)

    def test_strict_project_never_falls_back_to_cloud(self) -> None:
        dictation._local.update({"other-1", "other-2"})

        async def strict_project(email: str, org: str, project: str) -> str:
            self.assertEqual(email, "member@example.test")
            return "strict"

        scope = {"org": str(uuid4()), "project": str(uuid4())}
        with (
            patch.object(dictation, "local_installed", lambda: True),
            patch.object(dictation, "project_privacy", strict_project),
        ):
            result = self.start(scope=scope)
        self.assertEqual(result[0]["code"], "local_busy")
        self.assertEqual(result[0]["privacy"], "strict")
        self.assertEqual(FakeProvider.instances, [])
        self.assertNotIn("user-1", dictation._active)

    def test_cloud_pins_only_measured_languages(self) -> None:
        P = realtime_transcription.ProviderEvent
        for code, pinned in (("nl", "nl"), ("de", "de"), ("ja", None), ("xx", None)):
            FakeProvider.instances = []
            FakeProvider.script = [P("done", seconds=0.1)]
            result = self.start(engine="cloud", dictation_language=code)
            self.assertEqual(result[0]["type"], "ready")
            self.assertEqual(FakeProvider.instances[0].options["language"], pinned)

    def test_the_interface_language_never_pins_the_engine(self) -> None:
        # An English interface pinned French speech to English: the provider returned
        # nothing (dictation pilot, 2026-11-17). Only a chosen dictation language pins.
        P = realtime_transcription.ProviderEvent
        FakeProvider.script = [P("done", seconds=0.1)]
        result = self.start(engine="cloud", language="en")
        self.assertEqual(result[0]["type"], "ready")
        self.assertIsNone(FakeProvider.instances[0].options["language"])

    def test_the_cloud_engine_waits_1200_ms_by_default(self) -> None:
        os.environ.pop("ATLAS_DICTATION_DELAY_MS", None)
        self.assertEqual(dictation.Limits.current().delay_ms, 1200)

    def test_the_final_pass_text_is_sent_once_at_stop_and_billed(self) -> None:
        P = realtime_transcription.ProviderEvent
        FakeProvider.script = [
            P("turn_start", time_s=0.1),
            P("delta", "Euh bonjour à tous"),
            P("turn_end", time_s=1.0),
            P("done", seconds=2.0),
        ]
        calls: list[dict[str, Any]] = []

        async def batch(audio: bytes, **options: Any) -> dict[str, Any]:
            calls.append({"audio": audio} | options)
            return {
                "text": "Euh, bonjour à tous.",
                "language": "fr",
                "duration": 2.0,
                "cost_micro": 134,
            }

        with (
            patch.dict(os.environ, {"ATLAS_DICTATION_FINAL_PASS": "on"}),
            patch.object(transcription, "transcribe", batch),
        ):
            result = self.start(audio=b"\0\0" * 32000, engine="cloud")
        kinds = [message["type"] for message in result]
        # Live text for the panel (hesitation removed by rule), then one final text.
        self.assertIn({"type": "final", "index": 0, "text": "Bonjour à tous"}, result)
        transcript = [message for message in result if message["type"] == "transcript"]
        self.assertEqual(len(transcript), 1)
        self.assertEqual(transcript[0]["text"], "Bonjour à tous.")
        self.assertEqual(transcript[0]["outcome"], "done")
        self.assertLess(kinds.index("transcript"), kinds.index("done"))
        self.assertEqual(result[-1]["final_pass"], "done")
        # The whole recording, as WAV, language detected (none chosen).
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["audio"].startswith(b"RIFF"))
        self.assertEqual(len(calls[0]["audio"]), 44 + 64000)
        self.assertIsNone(calls[0]["language"])
        # Realtime 2 s (0.000334 EUR, rounded up) plus the final pass (0.000134 EUR).
        self.assertEqual(
            result[-1]["cost_eur"], str(Decimal("0.000334") + Decimal("0.000134"))
        )

    def test_a_failed_final_pass_is_logged_and_never_hidden(self) -> None:
        P = realtime_transcription.ProviderEvent
        FakeProvider.script = [P("delta", "bonjour"), P("done", seconds=1.0)]

        async def broken(audio: bytes, **options: Any) -> dict[str, Any]:
            raise transcription.ProviderUnavailable()

        with (
            patch.dict(os.environ, {"ATLAS_DICTATION_FINAL_PASS": "on"}),
            patch.object(transcription, "transcribe", broken),
            self.assertLogs("atlas.dictation", "WARNING") as logged,
        ):
            result = self.start(audio=b"\0\0" * 16000, engine="cloud")
        transcript = [message for message in result if message["type"] == "transcript"]
        self.assertEqual(transcript, [transcript[0]])
        self.assertIsNone(transcript[0]["text"])
        self.assertEqual(transcript[0]["outcome"], "failed")
        self.assertEqual(result[-1]["final_pass"], "failed")
        self.assertIn("dictation_final_pass_failed", logged.output[0])
        self.assertNotIn("bonjour", "".join(logged.output))

    def session(self, steps: list[object], **fields: object) -> list[dict[str, Any]]:
        """A session driven step by step: bytes are audio, dicts are control messages."""
        self.client.cookies.set("token", "good")
        with (
            patch("services.orchestrator.dictation.token_user", signed_in),
            patch.object(realtime_transcription, "ProviderStream", FakeProvider),
        ):
            with self.client.websocket_connect(
                "/api/atlas/dictation/stream", headers=ORIGIN
            ) as socket:
                socket.send_text(json.dumps({"type": "start"} | fields))
                first = json.loads(socket.receive_text())
                if first["type"] != "ready":
                    return [first]
                for step in steps:
                    if isinstance(step, bytes):
                        for offset in range(0, len(step), 16000):
                            socket.send_bytes(step[offset : offset + 16000])
                    else:
                        socket.send_text(json.dumps(step))
                return [first] + messages(socket)

    def test_each_paused_piece_is_written_once_and_the_dictation_goes_on(self) -> None:
        P = realtime_transcription.ProviderEvent
        FakeProvider.script = [
            P("turn_start", time_s=0.1),
            P("delta", "Premier"),
            P("turn_end", time_s=1.0),
            P("done", seconds=2.0),
        ]
        calls: list[int] = []

        async def batch(audio: bytes, **options: Any) -> dict[str, Any]:
            calls.append(len(audio) - 44)
            text = "Premier." if len(calls) == 1 else "Second."
            return {"text": text, "language": "fr", "duration": 1.0, "cost_micro": 67}

        second = b"\1\0" * 16000
        with (
            patch.dict(os.environ, {"ATLAS_DICTATION_FINAL_PASS": "on"}),
            patch.object(transcription, "transcribe", batch),
        ):
            result = self.session(
                [b"\0\0" * 16000, {"type": "commit"}, second, {"type": "stop"}],
                engine="cloud",
            )
        pieces = [message for message in result if message["type"] == "transcript"]
        # Both sentences, in order, each once: the pause no longer ends the session.
        self.assertEqual(
            [(m["piece"], m["last"], m["text"]) for m in pieces],
            [(0, False, "Premier."), (1, True, "Second.")],
        )
        self.assertEqual(calls, [32000, 32000])
        self.assertEqual(result[-1]["type"], "done")
        self.assertEqual(
            result[-1]["cost_eur"], str(Decimal("0.000334") + Decimal("0.000134"))
        )

    def test_a_commit_without_new_speech_makes_no_piece(self) -> None:
        P = realtime_transcription.ProviderEvent
        FakeProvider.script = [P("done", seconds=1.0)]
        result = self.session(
            [{"type": "commit"}, b"\0\0" * 16000, {"type": "stop"}], engine="cloud"
        )
        self.assertNotIn("transcript", [message["type"] for message in result])
        self.assertEqual(result[-1]["type"], "done")

    def test_no_last_cleanup_when_the_final_pass_replaces_the_live_text(self) -> None:
        P = realtime_transcription.ProviderEvent
        FakeProvider.script = [P("delta", "bonjour"), P("done", seconds=1.0)]
        cleaned: list[str] = []

        async def clean(self: Any, context: str, raw: str, **options: Any) -> Any:
            cleaned.append(raw)
            return dictation_cleanup.Outcome(raw, "accepted")

        async def batch(audio: bytes, **options: Any) -> dict[str, Any]:
            return {"text": "Bonjour.", "language": "fr", "duration": 1.0}

        async def broken(audio: bytes, **options: Any) -> dict[str, Any]:
            raise transcription.ProviderUnavailable()

        environment = {
            "ATLAS_DICTATION_FINAL_PASS": "on",
            "ATLAS_DICTATION_CLEANUP": "1",
        }
        with (
            patch.dict(os.environ, environment),
            patch.object(dictation_cleanup.Cleaner, "clean", clean),
        ):
            with patch.object(transcription, "transcribe", batch):
                self.start(audio=b"\0\0" * 16000, engine="cloud", cleanup=True)
            self.assertEqual(cleaned, [])
            # The final pass failed: the live text is written, so it is cleaned up.
            with patch.object(transcription, "transcribe", broken):
                result = self.start(audio=b"\0\0" * 16000, engine="cloud", cleanup=True)
        self.assertEqual(cleaned, ["bonjour"])
        kinds = [message["type"] for message in result]
        self.assertLess(kinds.index("cleaned"), kinds.index("done"))

    def test_ready_says_where_audio_and_text_go(self) -> None:
        FakeProvider.script = [realtime_transcription.ProviderEvent("done")]
        with patch.dict(os.environ, {"ATLAS_DICTATION_CLEANUP": "1"}):
            ready = self.start(engine="cloud", cleanup=True)[0]
        self.assertEqual((ready["audio_out"], ready["text_out"]), (True, True))
        with (
            patch.dict(os.environ, {"ATLAS_DICTATION_FINAL_PASS": "on"}),
            patch.object(dictation, "local_installed", lambda: True),
            patch.object(dictation, "batch_configured", lambda: True),
        ):
            self.assertEqual(dictation.final_pass_engine("standard"), "batch")
            self.assertEqual(dictation.final_pass_engine("strict"), "local")

    def test_each_session_leaves_one_journal_line_without_text(self) -> None:
        P = realtime_transcription.ProviderEvent
        FakeProvider.script = [P("delta", "bonjour secret"), P("done", seconds=1.0)]
        with self.assertLogs("atlas.dictation", "WARNING") as logged:
            self.start(audio=b"\0\0" * 16000, engine="cloud")
        lines = [line for line in logged.output if "dictation_session" in line]
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0].split(":", 2)[2])
        self.assertEqual(
            {k: record[k] for k in ("outcome", "privacy", "engine", "pieces")},
            {"outcome": "done", "privacy": "standard", "engine": "cloud", "pieces": 1},
        )
        self.assertIsNotNone(record["drain_seconds"])
        self.assertNotIn("bonjour", lines[0])
        self.assertNotIn("user-1", lines[0])

    def test_config_reports_the_flag_to_signed_in_users(self) -> None:
        async def user(request: object) -> str:
            return "user-1"

        with patch("services.orchestrator.dictation.browser_user", user):
            self.assertEqual(
                self.client.get("/api/atlas/dictation/config").json()["enabled"], True
            )
            with patch.dict(os.environ, {"ATLAS_REALTIME_DICTATION": "0"}):
                self.assertEqual(
                    self.client.get("/api/atlas/dictation/config").json(),
                    {"enabled": False},
                )
        self.assertEqual(
            self.client.get("/api/atlas/dictation/config").status_code, 401
        )


class EngineChoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {"ATLAS_DICTATION_ENGINES": "local,cloud", "MISTRAL_API_KEY": "test-key"},
        )
        self.environment.start()
        self.limits = dictation.Limits.current()
        dictation._local.clear()

    def tearDown(self) -> None:
        self.environment.stop()
        dictation._local.clear()

    def choose(self, requested: object, privacy: str = "standard") -> tuple[str, bool]:
        return dictation.choose_engine(requested, privacy, self.limits)

    def test_local_is_the_default_until_its_cap(self) -> None:
        with patch.object(dictation, "local_installed", lambda: True):
            self.assertEqual(self.choose("auto"), ("local", False))
            dictation._local.update({"a", "b"})
            self.assertEqual(self.choose("auto"), ("cloud", True))
            with self.assertRaises(dictation.Refusal) as refused:
                self.choose("local")
            self.assertEqual(refused.exception.body["code"], "local_busy")

    def test_local_cap_is_a_setting(self) -> None:
        with patch.dict(os.environ, {"ATLAS_DICTATION_LOCAL_MAX_SESSIONS": "1"}):
            self.limits = dictation.Limits.current()
        dictation._local.add("a")
        with patch.object(dictation, "local_installed", lambda: True):
            self.assertEqual(self.choose("auto"), ("cloud", True))

    def test_strict_privacy_keeps_audio_local(self) -> None:
        with patch.object(dictation, "local_installed", lambda: True):
            self.assertEqual(self.choose("auto", "strict"), ("local", False))
            dictation._local.update({"a", "b"})
            with self.assertRaises(dictation.Refusal) as refused:
                self.choose("auto", "strict")
            self.assertEqual(refused.exception.body["code"], "local_busy")
            self.assertEqual(refused.exception.body["privacy"], "strict")
            with self.assertRaises(dictation.Refusal) as refused:
                self.choose("cloud", "strict")
            self.assertEqual(
                refused.exception.body["code"], "strict_privacy_unavailable"
            )
        with patch.object(dictation, "local_installed", lambda: False):
            with self.assertRaises(dictation.Refusal) as refused:
                self.choose("auto", "strict")
            self.assertEqual(refused.exception.body["code"], "engine_unavailable")
            self.assertEqual(self.choose("auto"), ("cloud", False))


class LanguageTests(unittest.TestCase):
    def test_any_iso_code_is_kept_and_the_rest_is_dropped(self) -> None:
        for value, code in (("nl", "nl"), ("pt-BR", "pt"), ("FR", "fr"), ("", None)):
            self.assertEqual(dictation.spoken_language(value), code)
        for other in (None, 3, "fra", "x1", "é"):
            self.assertIsNone(dictation.spoken_language(other))

    def test_a_language_the_local_engine_lacks_goes_to_cloud_outside_strict(
        self,
    ) -> None:
        limits = dictation.Limits.current()
        with (
            patch.dict(
                os.environ,
                {"ATLAS_DICTATION_ENGINES": "local,cloud", "MISTRAL_API_KEY": "k"},
            ),
            patch.object(dictation, "local_installed", lambda: True),
        ):
            for code in ("fr", "en", "de", "es", "it", "nl", "pl", None):
                self.assertEqual(
                    dictation.choose_engine("auto", "standard", limits, code),
                    ("local", False),
                )
            self.assertEqual(
                dictation.choose_engine("auto", "standard", limits, "ja"),
                ("cloud", False),
            )
            self.assertEqual(
                dictation.choose_engine("auto", "strict", limits, "ja"),
                ("local", False),
            )


class PrivacyTests(unittest.TestCase):
    user = dictation.SocketUser("user-1", "member@example.test")

    def setUp(self) -> None:
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ATLAS_CONVERSATION_PRIVACY_DB": str(Path(root) / "levels.sqlite"),
                    "ATLAS_PRIVACY_MINIMUM": "",
                },
            )
        )

    def privacy(self, start: dict[str, object], setting: object) -> str:
        async def read(email: str, org: str, project: str) -> str:
            if isinstance(setting, Exception):
                raise setting
            return str(setting)

        with patch.object(dictation, "project_privacy", read):
            return asyncio.run(dictation.session_privacy(start, self.user))

    def test_the_project_setting_decides_and_the_browser_cannot_lift_it(self) -> None:
        scope = {"org": str(uuid4()), "project": str(uuid4())}
        self.assertEqual(self.privacy({}, "strict"), "standard")
        self.assertEqual(self.privacy({"privacy": "strict"}, "standard"), "strict")
        self.assertEqual(self.privacy({"scope": scope}, "strict"), "strict")
        self.assertEqual(
            self.privacy({"scope": scope, "privacy": "standard"}, "strict"), "strict"
        )
        self.assertEqual(self.privacy({"scope": scope}, "standard"), "standard")

    def test_a_new_conversation_follows_the_level_chosen_before_its_first_message(
        self,
    ) -> None:
        from services.orchestrator import conversation_privacy

        with conversation_privacy.connection() as db:
            db.execute(
                "INSERT INTO conversation_privacy VALUES (?,?,?,0,0)",
                (self.user.subject, "", "strict"),
            )
        # The browser sends no id on a new conversation (staging 2026-11-17: it ran as
        # Standard), or an id Atlas has not seen yet: both show the pending choice.
        for chat in ("", None, "chat-not-seen-yet"):
            with self.subTest(chat=chat):
                start: dict[str, object] = {"type": "start", "chat": chat}
                self.assertEqual(self.privacy(start, "standard"), "strict")
        self.assertEqual(
            self.privacy({"type": "start", "chat": "../x"}, "standard"), "strict"
        )
        # The level the selector shows raises it too, even before the store has it.
        with conversation_privacy.connection() as db:
            db.execute("DELETE FROM conversation_privacy")
        self.assertEqual(self.privacy({"chat": ""}, "standard"), "standard")
        self.assertEqual(
            self.privacy({"chat": "", "privacy": "strict"}, "standard"), "strict"
        )

    def test_an_unreadable_conversation_store_counts_as_strict(self) -> None:
        with patch.dict(
            os.environ, {"ATLAS_CONVERSATION_PRIVACY_DB": "/proc/atlas/levels.sqlite"}
        ):
            self.assertEqual(self.privacy({"chat": ""}, "standard"), "strict")

    def test_an_unreadable_project_counts_as_strict(self) -> None:
        scope = {"org": str(uuid4()), "project": str(uuid4())}
        self.assertEqual(
            self.privacy({"scope": scope}, ProjectError(404, "not_found")), "strict"
        )
        self.assertEqual(
            self.privacy({"scope": {"org": "x", "project": "y"}}, "standard"), "strict"
        )
        no_email = dictation.SocketUser("user-1", "")
        with patch.object(dictation, "project_privacy", lambda *a: None):
            self.assertEqual(
                asyncio.run(dictation.session_privacy({"scope": scope}, no_email)),
                "strict",
            )


class LocalAvailabilityTests(unittest.TestCase):
    def test_a_configured_model_directory_must_exist(self) -> None:
        with (
            patch("importlib.util.find_spec", lambda name: object()),
            tempfile.TemporaryDirectory() as directory,
        ):
            with patch.dict(os.environ, {"ATLAS_DICTATION_LOCAL_MODEL_DIR": directory}):
                os.environ.pop("ATLAS_DICTATION_LOCAL_VAD", None)
                self.assertTrue(streaming.available())
            missing = str(Path(directory) / "missing")
            with patch.dict(os.environ, {"ATLAS_DICTATION_LOCAL_MODEL_DIR": missing}):
                self.assertFalse(streaming.available())


class ChatButtonContractTests(unittest.TestCase):
    """The chat voice button must not touch Open WebUI's Svelte trees (#334, #335)."""

    def block(self) -> str:
        script = Path("services/orchestrator/chat-ui.js").read_text()
        start = script.index("// Real-time dictation (ATLAS_REALTIME_DICTATION)")
        return script[start:]

    def test_button_and_panel_live_in_the_body_and_never_observe_mutations(
        self,
    ) -> None:
        block = self.block()
        self.assertNotIn("MutationObserver", block)
        self.assertEqual(
            sorted(set(re.findall(r"(\w+)\.append\((?:button|panel)\)", block))),
            ["body"],
        )
        self.assertIn("document.body.append(button)", block)
        self.assertIn("document.body.append(panel)", block)
        self.assertIn("ResizeObserver", block)
        # One write, through the composer's own editing path.
        self.assertEqual(block.count('execCommand("insertText"'), 1)
        self.assertIn("/api/atlas/dictation/config", block)
        for language in ("en", "fr", "de", "es", "it", "nl"):
            self.assertIn(f"    {language}: {{ dictate:", block)

    def test_native_microphone_is_hidden_by_css_only_when_enabled(self) -> None:
        css = Path("services/orchestrator/chat-ui.css").read_text()
        self.assertIn(
            'html[data-atlas-dictation="on"] #voice-input-button { visibility:hidden!important;',
            css,
        )
        self.assertIn('dataset.atlasDictation = "on"', self.block())


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


class FinalPassTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {
                "ATLAS_DICTATION_ENGINES": "local,cloud",
                "MISTRAL_API_KEY": "test-key",
                "ATLAS_DICTATION_FINAL_PASS": "on",
            },
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()

    def test_strict_and_enhanced_keep_the_final_pass_on_the_owned_engine(self) -> None:
        windows: list[int] = []

        async def owned(pcm: bytes, speech: object = None) -> str:
            windows.append(len(pcm))
            return "Hum, le rapport est prêt."

        async def trap(*args: object, **options: object) -> dict[str, Any]:
            raise AssertionError("audio left Atlas")

        with (
            patch.object(dictation, "local_installed", lambda: True),
            patch.object(streaming, "transcribe_windows", owned),
            patch.object(transcription, "transcribe", trap),
        ):
            for level in ("strict", "enhanced"):
                result = asyncio.run(
                    dictation.final_pass(b"\0\0" * 16000, level, "fr", None)
                )
                self.assertEqual(
                    (result.engine, result.outcome, result.text, result.cost_micro),
                    ("local", "done", "Le rapport est prêt.", 0),
                )
        self.assertEqual(windows, [32000, 32000])

    def test_standard_uses_the_batch_provider_and_pins_only_a_chosen_language(
        self,
    ) -> None:
        seen: list[object] = []

        async def batch(audio: bytes, **options: Any) -> dict[str, Any]:
            seen.append(options["language"])
            return {"text": "Um, the report is ready.", "language": "en"}

        with patch.object(transcription, "transcribe", batch):
            result = asyncio.run(
                dictation.final_pass(b"\0\0" * 16000, "standard", None, None)
            )
            asyncio.run(dictation.final_pass(b"\0\0" * 16000, "standard", "de", None))
        self.assertEqual(
            (result.engine, result.text), ("batch", "The report is ready.")
        )
        self.assertEqual(result.cost_micro, transcription.price_micro(1.0))
        self.assertEqual(seen, [None, "de"])

    def test_late_final_pass_is_reported_as_late(self) -> None:
        async def slow(audio: bytes, **options: Any) -> dict[str, Any]:
            await asyncio.sleep(5)
            return {"text": "trop tard"}

        with (
            patch.dict(os.environ, {"ATLAS_DICTATION_FINAL_PASS_MAX_S": "0.05"}),
            patch.object(transcription, "transcribe", slow),
            patch.object(dictation, "final_pass_deadline", lambda seconds: 0.05),
            self.assertLogs("atlas.dictation", "WARNING"),
        ):
            result = asyncio.run(
                dictation.final_pass(b"\0\0" * 16000, "standard", None, None)
            )
        self.assertEqual((result.text, result.outcome), (None, "late"))

    def test_short_or_disabled_recordings_skip_the_final_pass(self) -> None:
        self.assertEqual(
            asyncio.run(
                dictation.final_pass(b"\0\0" * 100, "standard", None, None)
            ).outcome,
            "skipped",
        )
        with patch.dict(os.environ, {"ATLAS_DICTATION_FINAL_PASS": "off"}):
            self.assertIsNone(dictation.final_pass_engine("standard"))


class WindowTests(unittest.TestCase):
    hop = streaming.VAD_HOP

    def test_windows_cut_at_the_longest_pause_and_skip_silence(self) -> None:
        speech = [0.9] * 400 + [0.0] * 40 + [0.9] * 400 + [0.0] * 10 + [0.9] * 300
        windows = streaming.plan_windows(speech, len(speech) * self.hop)
        self.assertEqual(len(windows), 3)
        # The first cut falls in the 40-frame pause, not in speech.
        self.assertTrue(
            400 * self.hop <= windows[0][1] <= windows[1][0] <= 440 * self.hop
        )
        for begin, end in windows:
            self.assertLessEqual(
                end - begin, streaming.WINDOW_MAX * streaming.SAMPLE_RATE
            )
        self.assertEqual(streaming.plan_windows([0.0] * 100, 100 * self.hop), [])

    def test_speech_without_pause_is_cut_at_the_window_limit(self) -> None:
        windows = streaming.plan_windows([0.9] * 1000, 1000 * self.hop)
        limit = int(streaming.WINDOW_MAX * streaming.SAMPLE_RATE) // self.hop * self.hop
        self.assertEqual(windows, [(0, limit), (limit, 1000 * self.hop)])

    def test_windows_are_decoded_and_joined_in_order(self) -> None:
        speech = [0.9] * 400 + [0.0] * 40 + [0.9] * 400
        pcm = bytes(len(speech) * self.hop * 2)
        decoded: list[int] = []

        def decode(samples: Any) -> str:
            decoded.append(len(samples))
            return f"part{len(decoded)} "

        text = asyncio.run(streaming.transcribe_windows(pcm, speech, decode))
        self.assertEqual(sorted(text.split()), ["part1", "part2"])
        self.assertEqual(len(decoded), 2)

    def test_an_empty_window_with_speech_is_split_and_decoded_again(self) -> None:
        speech = [0.9] * 200 + [0.0] * 30 + [0.9] * 200
        pcm = bytes(len(speech) * self.hop * 2)
        lengths: list[int] = []

        def decode(samples: Any) -> str:
            lengths.append(len(samples))
            return "" if len(lengths) == 1 else "half"

        text = asyncio.run(streaming.transcribe_windows(pcm, speech, decode))
        self.assertEqual(text, "half half")
        self.assertEqual(len(lengths), 3)
        self.assertEqual(lengths[1] + lengths[2], lengths[0])

    def test_an_utterance_closes_after_1_2_s_of_silence_by_default(self) -> None:
        os.environ.pop("ATLAS_DICTATION_LOCAL_END_SILENCE_S", None)
        import importlib

        fresh = importlib.reload(streaming)
        self.assertEqual(fresh.END_SILENCE, 1.2)


class HesitationTests(unittest.TestCase):
    def test_each_language_loses_its_hesitations_and_keeps_its_words(self) -> None:
        cases = [
            ("Euh, je pense que, euh, c'est bon.", None, "Je pense que, c'est bon."),
            (
                "Bonjour, heu, je voudrais une table.",
                "fr",
                "Bonjour, je voudrais une table.",
            ),
            (
                "Hum. On commence quand vous voulez.",
                "fr",
                "On commence quand vous voulez.",
            ),
            (
                "So, um, I think the meeting is at noon and we are late.",
                None,
                "So, I think the meeting is at noon and we are late.",
            ),
            (
                "Ähm, ich glaube das ist nicht gut.",
                None,
                "Ich glaube das ist nicht gut.",
            ),
            ("Ehm, allora, eh, va bene così.", "it", "Allora, va bene così."),
            ("Pues, eh, no sé.", "es", "Pues, no sé."),
            ("je pense euuuh.", None, "je pense."),
        ]
        for text, language, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    dictation_text.remove_hesitations(text, language), expected
                )

    def test_words_of_other_languages_are_never_taken_for_hesitations(self) -> None:
        for text, language in (
            ("Eu tenho um carro e uma casa.", "pt"),
            ("Ele mora em Lisboa.", "pt"),
            ("Eh bien, je pense que oui.", "fr"),
            ("The hum of the engine is loud.", "en"),
            ("La pièce mesure 5 mm.", "fr"),
            ("Er kommt morgen.", "de"),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    dictation_text.remove_hesitations(text, language), text
                )
        # Without a known language only universal sounds go.
        self.assertEqual(dictation_text.remove_hesitations("um", None), "um")

    def test_the_language_is_guessed_only_with_evidence(self) -> None:
        self.assertEqual(
            dictation_text.guess_language("je pense que le rapport est dans la boîte"),
            "fr",
        )
        self.assertEqual(
            dictation_text.guess_language("the report is in the box and it is ready"),
            "en",
        )
        self.assertIsNone(dictation_text.guess_language("Atlas"))


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

    def test_open_text_keeps_its_last_word_as_heard(self) -> None:
        # The engine may still be inside the word or split it across two segments.
        self.assertFalse(
            dictation_cleanup.accepted(
                "kannst du mir den bericht sch",
                "Kannst du mir den Bericht schicken?",
                open_end=True,
            )
        )
        self.assertTrue(
            dictation_cleanup.accepted(
                "kannst du mir den bericht sch",
                "Kannst du mir den Bericht sch",
                open_end=True,
            )
        )
        self.assertTrue(
            dictation_cleanup.accepted(
                "envoie le raport", "Envoie le rapport.", open_end=False
            )
        )
        self.assertFalse(
            dictation_cleanup.accepted(
                "envoie le raport", "Envoie le rapport.", open_end=True
            )
        )

    def test_the_last_word_is_put_back_and_other_fixes_kept(self) -> None:
        self.assertEqual(
            dictation_cleanup.with_last_word(
                "Kannst du mir den Bericht schicken?", "kannst du mir den bericht sch"
            ),
            "Kannst du mir den Bericht sch?",
        )
        self.assertEqual(
            dictation_cleanup.with_last_word("Envoie le rapport.", "envoie le raport"),
            "Envoie le raport.",
        )

    def test_guard_rejects_a_deleted_word_but_not_a_hesitation(self) -> None:
        self.assertFalse(
            dictation_cleanup.accepted(
                "il a parlé de la vertu du travail bien fait",
                "Il a parlé de la du travail bien fait.",
            )
        )
        self.assertTrue(
            dictation_cleanup.accepted(
                "il a parlé euh de la vertu du travail bien fait",
                "Il a parlé de la vertu du travail bien fait.",
            )
        )

    def test_guard_allows_restoring_a_dropped_word_inside_the_text(self) -> None:
        raw = "merci d envoyer le colis à avant midi demain matin sans faute"
        self.assertTrue(
            dictation_cleanup.accepted(
                raw,
                "Merci d'envoyer le colis à l'adresse avant midi demain matin sans faute.",
            )
        )
        # Never at either end, where inventions happen ("Bonjour, ...", "... complet").
        self.assertFalse(
            dictation_cleanup.accepted(
                raw,
                "Bonjour, merci d'envoyer le colis à avant midi demain matin sans faute.",
            )
        )

    def test_alignment_lists_operations_in_order(self) -> None:
        self.assertEqual(
            dictation_cleanup.alignment(["a", "b", "c"], ["a", "c"]),
            [("same", 0, 0), ("del", 1, -1), ("same", 2, 1)],
        )
        self.assertEqual(
            dictation_cleanup.alignment(["a", "c"], ["a", "x", "c"]),
            [("same", 0, 0), ("ins", -1, 1), ("same", 1, 2)],
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
