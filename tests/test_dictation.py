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
from unittest.mock import patch
from uuid import uuid4

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

    def start(self, **fields: object) -> list[dict[str, Any]]:
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
            result = self.start(engine="cloud", language=code)
            self.assertEqual(result[0]["type"], "ready")
            self.assertEqual(FakeProvider.instances[0].options["language"], pinned)

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
