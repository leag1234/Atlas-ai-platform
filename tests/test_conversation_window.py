"""M25 history boundaries use model windows and disclose safe numeric evidence."""

import unittest
import os
import tempfile
from fastapi.testclient import TestClient
from services.orchestrator.chat_api import app
from unittest.mock import patch
from pydantic import ValidationError
from packages.profiles import PROFILES
from services.orchestrator.chat_schema import ChatRequest


class ConversationWindowTests(unittest.TestCase):
    def test_long_text_is_not_a_character_limit(self) -> None:
        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "word " * 50000}]}
        )
        self.assertEqual(len(request.messages[0].text), 250000)

    def test_selected_window_and_reservations(self) -> None:
        with patch(
            "services.orchestrator.chat_schema.context_window", return_value=10000
        ):
            request = ChatRequest.model_validate(
                {"max_tokens": 1, "messages": [{"role": "user", "content": "hello"}]}
            )
            self.assertEqual(request.max_tokens, 1)
            with self.assertRaises(ValidationError) as caught:
                ChatRequest.model_validate(
                    {"messages": [{"role": "user", "content": "word " * 2000}]}
                )
        error = caught.exception.errors()[0]
        self.assertEqual(error["type"], "conversation_window_exceeded")
        self.assertEqual(error["ctx"]["window"], 10000)
        self.assertEqual(error["ctx"]["reserved"], 6000)
        self.assertGreater(error["ctx"]["tokens"], 0)
        self.assertIn("tool allowance", error["msg"])

    def test_over_window_and_unicode(self) -> None:
        for profile in PROFILES:
            with (
                self.subTest(profile=profile),
                self.assertRaises(ValidationError) as caught,
            ):
                ChatRequest.model_validate(
                    {
                        "model": profile,
                        "messages": [{"role": "user", "content": "漢字 " * 150000}],
                    }
                )
            error = caught.exception.errors()[0]
            self.assertEqual(error["type"], "conversation_window_exceeded")
            self.assertGreater(error["ctx"]["tokens"], error["ctx"]["window"])

    def test_public_refusal_reports_measurements(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "word " * 270000}]},
            )
        self.assertEqual(response.status_code, 400)
        text = response.text
        for expected in ("270004", "262144", "6000", "8192", "tokens"):
            self.assertIn(expected, text)
        self.assertNotIn("value_error", text)
