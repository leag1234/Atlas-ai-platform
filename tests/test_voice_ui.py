"""Browser voice controls: config route, forwarding and UI contracts."""

import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from services.orchestrator.chat_api import app
from services.orchestrator.ui_injection import ATLAS_UI_TAGS, inject_atlas_ui


async def signed_in(request) -> str:
    return "user-1"


class VoiceConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def test_config_reports_each_capability_independently(self) -> None:
        with patch.dict(os.environ, {"ATLAS_STT": "1", "ATLAS_TTS": "0"}, clear=False):
            os.environ.pop("ATLAS_STABLE", None)
            response = self.client.get("/api/atlas/voice/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"stt": True, "tts": False})
        with patch.dict(os.environ, {"ATLAS_STT": "0", "ATLAS_TTS": "1"}, clear=False):
            response = self.client.get("/api/atlas/voice/config")
        self.assertEqual(response.json(), {"stt": False, "tts": True})

    def test_speech_routes_require_a_signed_in_user(self) -> None:
        with patch.dict(os.environ, {"ATLAS_STT": "1", "ATLAS_TTS": "1"}, clear=False):
            for path in ("/api/atlas/voice/transcriptions", "/api/atlas/voice/speech"):
                response = self.client.post(
                    path, content=b"{}", headers={"Origin": "http://testserver"}
                )
                with self.subTest(path=path):
                    self.assertEqual(response.status_code, 401)

    def test_transcriptions_refused_when_stt_is_off(self) -> None:
        with patch.dict(os.environ, {"ATLAS_STT": "0"}, clear=False), patch(
            "services.orchestrator.voice_ui.browser_user", signed_in
        ):
            response = self.client.post(
                "/api/atlas/voice/transcriptions",
                content=b"",
                headers={
                    "Content-Type": "multipart/form-data; boundary=x",
                    "Origin": "http://testserver",
                },
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "stt_unavailable")

    def test_speech_refused_when_tts_is_off(self) -> None:
        with patch.dict(os.environ, {"ATLAS_TTS": "0"}, clear=False), patch(
            "services.orchestrator.voice_ui.browser_user", signed_in
        ):
            response = self.client.post(
                "/api/atlas/voice/speech",
                json={"model": "atlas-tts", "input": "hello"},
                headers={"Origin": "http://testserver"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "tts_unavailable")

    def test_client_asset_is_served(self) -> None:
        response = self.client.get("/api/atlas/voice/client.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("voice/transcriptions", response.text)


class VoiceUIContractTests(unittest.TestCase):
    def test_injection_loads_the_voice_script_once(self) -> None:
        shell = b"<html><head></head><body>shell</body></html>"
        injected = inject_atlas_ui(shell)
        self.assertEqual(injected.count(b"/api/atlas/voice/client.js"), 1)
        self.assertIn(b"/api/atlas/voice/client.js", ATLAS_UI_TAGS)

    def test_chat_ui_keeps_the_dictation_button_visible(self) -> None:
        css = Path("services/orchestrator/chat-ui.css").read_text()
        self.assertIn("#message-input-container > [data-atlas-voice]", css)
        self.assertIn("[data-atlas-read]", css)

    def test_project_composer_has_a_dictation_button_and_locale_labels(self) -> None:
        script = Path("services/orchestrator/project_mode.js").read_text()
        self.assertIn("data-voice", script)
        self.assertIn("/api/atlas/voice/transcriptions", script)
        locale = Path("services/orchestrator/project_locale.js").read_text()
        for language in ("en", "fr", "de", "es", "it"):
            self.assertIn("dictate:", locale)
        self.assertEqual(locale.count("voiceUnavailable:"), 1)

    def test_rendering_report_matches_the_stylesheet(self) -> None:
        import hashlib

        rendering = json.loads(Path("reports/m24-rendering.json").read_text())
        digest = hashlib.sha256(
            Path("services/orchestrator/chat-ui.css").read_bytes()
        ).hexdigest()
        self.assertEqual(rendering["css_sha256"], digest)
