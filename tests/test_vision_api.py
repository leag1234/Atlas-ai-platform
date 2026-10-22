"""M12 must forward image bytes and keep them out of private journals."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from services.orchestrator.chat_api import app
from test_vision_schema import payload, picture


class VisionAPITests(unittest.TestCase):
    def test_image_reaches_vision_gateway_and_journal_is_text_only(self) -> None:
        url = picture()
        upstream = AsyncMock(
            return_value={
                "text": "A red square.",
                "usage": {"prompt_tokens": 100, "completion_tokens": 5},
                "cost_eur": "0.000021",
                "observation": {"provider": "escalade", "route": "complexe"},
            }
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch("services.orchestrator.model.GatewayModel.post", new=upstream),
            TestClient(app) as client,
        ):
            response = client.post("/v1/chat/completions", json=payload(url))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["choices"][0]["message"]["content"], "A red square."
            )
            self.assertEqual(upstream.await_count, 1)
            assert upstream.await_args is not None
            arguments = upstream.await_args.args
            self.assertTrue(arguments[0].endswith("/vision/complete"))
            self.assertIn(url, json.dumps(arguments[1]))
            rows = [
                json.loads(line)
                for p in Path(root).glob("*.jsonl")
                for line in p.read_text().splitlines()
            ]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["question"], "Décris cette image.")
            self.assertEqual(rows[0]["tokens"], {"in": 100, "out": 5})
            self.assertAlmostEqual(rows[0]["cout_eur"], 0.000021)
            self.assertNotIn(url.split(",")[1], json.dumps(rows))
            self.assertNotIn("base64", json.dumps(rows))

    def test_invalid_image_never_calls_gateway(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch(
                "services.orchestrator.model.GatewayModel.post", new=AsyncMock()
            ) as upstream,
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/chat/completions", json=payload("http://169.254.169.254/")
            )
            self.assertEqual(response.status_code, 400)
            upstream.assert_not_awaited()

    def test_large_body_rejected_before_provider(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch(
                "services.orchestrator.model.GatewayModel.post", new=AsyncMock()
            ) as upstream,
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/chat/completions", content=b" " * (6 * 1024 * 1024 + 1)
            )
            self.assertEqual(response.status_code, 413)
            upstream.assert_not_awaited()

    def test_error_reservation_and_image_echo_are_safe(self) -> None:
        from services.orchestrator.model import GatewayError

        url = picture()
        cases = [
            TimeoutError(),
            GatewayError("cost_budget", 504),
            {"text": "missing usage"},
            {
                "text": url.split(",")[1],
                "usage": {"prompt_tokens": 100, "completion_tokens": 5},
                "cost_eur": "0.000021",
                "observation": {"provider": "escalade", "route": "complexe"},
            },
        ]
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            TestClient(app) as client,
        ):
            for value, status in zip(cases, (504, 504, 502, 200), strict=True):
                upstream = (
                    AsyncMock(side_effect=value)
                    if isinstance(value, Exception)
                    else AsyncMock(return_value=value)
                )
                with patch(
                    "services.orchestrator.model.GatewayModel.post", new=upstream
                ):
                    response = client.post("/v1/chat/completions", json=payload(url))
                    self.assertEqual(response.status_code, status)
                    self.assertNotIn(url.split(",")[1], response.text)
            rows = [
                json.loads(line)
                for p in Path(root).glob("*.jsonl")
                for line in p.read_text().splitlines()
            ]
            self.assertEqual([row["cout_eur"] for row in rows[:3]], [0.10] * 3)
            self.assertEqual(rows[1]["erreurs"], ["cost_budget"])
            self.assertNotIn(url.split(",")[1], json.dumps(rows))
            self.assertEqual(rows[-1]["task_type"], "vision")
            self.assertEqual(rows[-1]["images"][0]["format"], "PNG")

    def test_sse_returns_sanitized_vision_answer(self) -> None:
        url, key = picture(), "a" * 64
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch(
                "services.orchestrator.model.GatewayModel.post",
                new=AsyncMock(
                    return_value={
                        "text": "Image " + url + " empreinte " + key,
                        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                        "cost_eur": "0.000024",
                        "observation": {"provider": "escalade", "route": "complexe"},
                    }
                ),
            ),
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/chat/completions", json={**payload(url), "stream": True}
            )
            events = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
            self.assertEqual(response.status_code, 200)
            self.assertFalse(any("error" in event for event in events))
            content = "".join(
                e["choices"][0]["delta"].get("content", "") for e in events
            )
            self.assertEqual(content, "Image [IMAGE] empreinte " + key)
            self.assertEqual(events[-1]["choices"][0]["finish_reason"], "stop")
            self.assertIn("data: [DONE]", response.text)
            self.assertNotIn(url.split(",")[1], response.text)
            rows = [
                json.loads(line)
                for p in Path(root).glob("*.jsonl")
                for line in p.read_text().splitlines()
            ]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["reponse"], content)
            self.assertNotIn("base64", json.dumps(rows))
