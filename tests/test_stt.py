import io
import os
from typing import cast
import unittest
from unittest.mock import patch
import wave

from fastapi.testclient import TestClient
from httpx import Response

from packages.ports import atlas_ports
from services.orchestrator import serving
from services.stt import service


class StubTranscriber:
    def __init__(self, text: str = "dictated text", language: str = "en") -> None:
        self.text = text
        self.language = language

    def transcribe(
        self, audio: io.BytesIO, language: str | None, prompt: str | None
    ) -> tuple[str, str]:
        return self.text, self.language


class STTContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stub = StubTranscriber()
        self.transcriber = patch.object(service, "transcriber", self.stub)
        self.duration = patch.object(service, "_duration", return_value=1.0)
        self.transcriber.start()
        self.duration.start()
        self.client = TestClient(service.app)

    def tearDown(self) -> None:
        self.client.close()
        self.duration.stop()
        self.transcriber.stop()

    def request(self, **fields: str) -> Response:
        data = {"model": "atlas-stt", **fields}
        return cast(
            Response,
            self.client.post(
                "/v1/audio/transcriptions",
                data=data,
                files={"file": ("capture.webm", b"audio", "audio/webm")},
            ),
        )

    def test_openai_contract_models_json_and_text(self) -> None:
        models = self.client.get("/v1/models")
        response = self.request(language="en")
        text = self.request(response_format="text")
        self.assertEqual(models.status_code, 200)
        self.assertEqual(models.json()["data"][0]["id"], "atlas-stt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"text": "dictated text"})
        self.assertEqual(text.status_code, 200)
        self.assertEqual(text.text, "dictated text")

    def test_rejects_non_audio_size_duration_and_language(self) -> None:
        non_audio = self.client.post(
            "/v1/audio/transcriptions",
            data={"model": "atlas-stt"},
            files={"file": ("notes.txt", b"not audio", "text/plain")},
        )
        language = self.request(language="pt")
        with patch.object(service, "_duration", return_value=120.1):
            too_long = self.request()
        with patch.object(service, "MAX_AUDIO_BYTES", 3):
            too_large = self.request()
        self.assertEqual(non_audio.status_code, 415)
        self.assertEqual(language.status_code, 400)
        self.assertEqual(too_long.status_code, 413)
        self.assertEqual(too_large.status_code, 413)

    def test_rejects_an_unsupported_automatically_detected_language(self) -> None:
        with patch.object(service, "transcriber", StubTranscriber(language="pt")):
            response = self.request()
        self.assertEqual(response.status_code, 400)

    def test_pyav_decodes_a_wav_before_transcription(self) -> None:
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16_000)
            wav.writeframes(b"\x00\x00" * 16_000)
        self.assertAlmostEqual(service._duration(audio.getvalue()), 1.0, places=2)


class STTConfigurationTests(unittest.TestCase):
    def test_port_registry_has_an_offsettable_stt_port(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PORT_BASE": "1100"}, clear=True):
            self.assertEqual(atlas_ports().port("stt"), 9122)

    def test_stable_instance_never_enables_stt_without_its_explicit_switch(
        self,
    ) -> None:
        with patch.dict(os.environ, {"ATLAS_STT": "1"}, clear=True):
            self.assertTrue(serving.stt_enabled())
        with patch.dict(
            os.environ, {"ATLAS_STT": "1", "ATLAS_STABLE": "1"}, clear=True
        ):
            self.assertFalse(serving.stt_enabled())
        with patch.dict(
            os.environ,
            {"ATLAS_STT": "1", "ATLAS_STABLE": "1", "ATLAS_STABLE_STT": "1"},
            clear=True,
        ):
            self.assertTrue(serving.stt_enabled())

    def test_webui_enables_microphone_only_with_stt(self) -> None:
        environment = {
            "ATLAS_PORT_BASE": "1100",
            "OPEN_TERMINAL_API_KEY": "test-terminal-key",
            "ATLAS_STT": "1",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "services.orchestrator.terminal_stack.network_gateway",
                return_value="172.18.0.1",
            ),
            patch("services.orchestrator.serving.docker_run") as docker_run,
        ):
            serving.webui("atlas-chat-ui", True)
        options = docker_run.call_args.args[2]
        self.assertIn("ENABLE_VOICE_INTERACTION=True", options)
        self.assertIn("AUDIO_STT_ENGINE=openai", options)
        self.assertIn("AUDIO_STT_MODEL=atlas-stt", options)
        self.assertTrue(any(value.endswith(":9122/v1") for value in options))
