"""Permanent M19 locks on model instructions and image settings."""

import hashlib
import json
from pathlib import Path
import re
import unittest

from packages.language import conversation_language, language_instruction
from services.orchestrator.chat_schema import ChatMessage


class SettingsLockTests(unittest.TestCase):
    def test_required_startup_configuration(self) -> None:
        from packages.configuration import (
            CHAT_REQUIRED_ENV,
            GPU_REQUIRED_ENV,
            IMAGE_REQUIRED_ENV,
            require_env,
        )
        from unittest.mock import patch
        import os

        self.assertEqual(
            set(CHAT_REQUIRED_ENV),
            {"ESCALATION_MODEL", "SCW_GENERATIVE_BASE_URL", "SCW_GENERATIVE_API_KEY"},
        )
        self.assertEqual(
            set(GPU_REQUIRED_ENV),
            {
                "SCW_DEFAULT_PROJECT_ID",
                "SCW_DEFAULT_ZONE",
                "LOCAL_MODEL",
                "GPU_CLIENT_IP",
                "GPU_MAX_EUR_H",
            },
        )
        self.assertEqual(
            set(IMAGE_REQUIRED_ENV),
            set(CHAT_REQUIRED_ENV) | (set(GPU_REQUIRED_ENV) - {"GPU_CLIENT_IP"}),
        )
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "GPU_CLIENT_IP"):
                require_env(GPU_REQUIRED_ENV)

    def test_capabilities_and_model_path_instructions(self) -> None:
        chat = Path("prompts/chat.txt").read_text().lower()
        for capability in (
            "image analysis",
            "image generation",
            "citations",
            "web",
            "calculation",
            "project memory",
            "mcp",
        ):
            self.assertIn(capability, chat)
        for name in (
            "chat",
            "vision",
            "web-chat",
            "followup",
            "project",
            "image-rewrite",
        ):
            self.assertRegex(
                Path(f"prompts/{name}.txt").read_text().lower(), r"language|answer in"
            )
        for language, name in [
            ("fr", "French"),
            ("en", "English"),
            ("de", "German"),
            ("es", "Spanish"),
            ("it", "Italian"),
        ]:
            self.assertIn("Answer in " + name, language_instruction(language))

    def test_worker_and_ci_wait(self) -> None:
        self.assertIn("max 180 attempts", Path("scripts/run_agent_auto.sh").read_text())
        worker = Path("services/model-gateway/image_worker.py").read_text()
        for key, value in [
            ("max_sequence_length", 512),
            ("height", 1024),
            ("width", 1024),
        ]:
            self.assertRegex(worker, rf"{key}\s*=\s*{value}\b")
        self.assertIn("secrets.randbits(32)", worker)
        self.assertIn("manual_seed(seed)", worker)
        watchdog = re.search(r"Timer\((\d+),", worker)
        self.assertIsNotNone(watchdog)
        assert watchdog
        self.assertGreaterEqual(int(watchdog[1]), 180)
        repository = json.loads(
            Path("services/model-gateway/image-model.json").read_text()
        )["repository"]
        self.assertEqual(
            hashlib.sha256(repository.encode()).hexdigest(),
            "f29a7ca86c33636eca6f4c35624831046e70fe66d1642caccbb44890ceb36e94",
        )

    def test_short_messages_inherit_history_then_locale(self) -> None:
        for text in ("décris cette image", "decris cette image", "DECRIS CETTE IMAGE"):
            self.assertEqual(
                conversation_language([ChatMessage(role="user", content=text)], "en"),
                "fr",
            )
        history = [ChatMessage(role="user", content="Bonjour, discutons en français.")]
        self.assertEqual(
            conversation_language(
                history + [ChatMessage(role="user", content="Describe this image")],
                "en",
            ),
            "fr",
        )
        self.assertEqual(
            conversation_language([ChatMessage(role="user", content="123 ?")], "it"),
            "it",
        )
        self.assertEqual(
            conversation_language(
                history
                + [
                    ChatMessage(
                        role="user",
                        content="Please describe the colors in this picture.",
                    )
                ],
                "fr",
            ),
            "en",
        )

    def test_english_image_request_does_not_fall_back_to_french(self) -> None:
        self.assertEqual(
            conversation_language(
                [ChatMessage(role="user", content="I would like to see a dragon")], "fr"
            ),
            "en",
        )

    def test_rewrite_declares_source_and_output_languages(self) -> None:
        from image_prompt import messages

        prompt = str(messages("un cube rouge", "fr")[0]["content"])
        self.assertIn('"source_language": "fr"', prompt)
        self.assertIn('"output_language": "en"', prompt)
