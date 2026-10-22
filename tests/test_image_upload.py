"""M20 public upload regressions preserve the strict provider allocation limits."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from services.orchestrator.chat_api import app
from test_vision_limits import noise, repeated
from test_vision_schema import picture


class UploadTests(unittest.TestCase):
    def test_two_images_one_large_png_produce_honest_edit_response(self) -> None:
        payload = repeated(picture(), 2)
        messages = payload["messages"]
        assert isinstance(messages, list)
        parts = messages[0]["content"]
        parts[0]["text"] = "intègre ces deux images"
        parts[1]["image_url"]["url"] = noise(1024)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
            patch(
                "services.orchestrator.model.GatewayModel.post",
                side_effect=AssertionError("unexpected provider call"),
            ),
            TestClient(app) as client,
        ):
            response = client.post("/v1/chat/completions", json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            text = response.json()["choices"][0]["message"]["content"]
            self.assertIn("pas prise en charge", text)
            self.assertIn("générer", text)
            rows = [
                json.loads(line)
                for p in Path(directory).glob("*.jsonl")
                for line in p.read_text().splitlines()
            ]
            self.assertEqual(rows[0]["question"], "intègre ces deux images")
            self.assertGreater(rows[0]["uploads"][0]["original_bytes"], 2097152)
            self.assertLessEqual(rows[0]["uploads"][0]["normalized_bytes"], 2097152)
            self.assertNotIn("base64", json.dumps(rows))

    def test_aggregate_limit_reports_measurement_without_context_error(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
            patch(
                "services.orchestrator.model.GatewayModel.post",
                side_effect=AssertionError("unexpected provider call"),
            ),
            TestClient(app) as client,
        ):
            response = client.post("/v1/chat/completions", json=repeated(noise(600), 4))
            self.assertEqual(response.status_code, 413)
            error = response.json()["error"]
            self.assertEqual(error["code"], "image_size_exceeded")
            self.assertIn("4194304", error["message"])
            self.assertIn("bytes", error["message"])
            rows = [
                json.loads(line)
                for p in Path(directory).glob("*.jsonl")
                for line in p.read_text().splitlines()
            ]
            self.assertEqual(rows[0]["rejection"]["message"], error["message"])

    def test_analysis_of_a_composite_is_not_an_edit_request(self) -> None:
        from unittest.mock import AsyncMock

        answer = {
            "text": "La première image est rouge, la seconde est bleue.",
            "usage": {"prompt_tokens": 100, "completion_tokens": 15},
            "cost_eur": "0.000023",
            "observation": {"provider": "escalade", "route": "complexe"},
        }
        for question in (
            "analyse ces deux images ensemble",
            "décris ce montage",
            "describe this edited picture",
        ):
            payload = repeated(picture(), 2)
            messages = payload["messages"]
            assert isinstance(messages, list)
            messages[0]["content"][0]["text"] = question
            with (
                tempfile.TemporaryDirectory() as directory,
                patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
                patch(
                    "services.orchestrator.model.GatewayModel.post",
                    new=AsyncMock(return_value=answer),
                ),
                TestClient(app) as client,
            ):
                response = client.post("/v1/chat/completions", json=payload)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.json()["choices"][0]["message"]["content"], answer["text"]
                )
