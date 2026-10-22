"""M26 bounded attachment requests and explicit provider/budget failures."""

import os
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from services.orchestrator.chat_api import app
from services.orchestrator.chat_schema import ChatRequest, ChatMessage
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Limits
from services.orchestrator.model import GatewayError
from packages.profiles import PROFILES


class BudgetDeliveryTests(unittest.TestCase):
    def test_attachment_deadline_is_bounded_in_loop_and_gateway(self) -> None:
        from agent_provider import AgentRequest
        from pydantic import ValidationError

        Limits(profile=PROFILES[0], has_attachments=True, wall_clock=300)
        with self.assertRaises(ValueError):
            Limits(profile=PROFILES[0], wall_clock=300)
        data = dict(
            profile=PROFILES[0],
            messages=[{"role": "user", "content": "Summarize"}],
            tools=[],
            timeout=300.0,
        )
        AgentRequest.model_validate({**data, "has_attachments": True})
        with self.assertRaisesRegex(ValidationError, "timeout_budget"):
            AgentRequest.model_validate(data)

    def test_local_budget_refusal_reaches_http_with_measurements(self) -> None:
        async def stopped(request: ChatRequest, item: Interaction) -> None:
            item.state = "stopped"
            item.erreurs.append("cost_budget")
            item.reponse = "cost_budget: measured 129453 microEUR; limit 21273 microEUR"

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch("services.orchestrator.chat_api.process", stopped),
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "Explain"}]},
            )
        self.assertEqual(response.status_code, 429)
        self.assertIn("129453", response.text)
        self.assertIn("21273", response.text)
        self.assertEqual(response.json()["error"]["code"], "cost_budget")

    def test_gateway_budget_refusal_is_not_timeout(self) -> None:
        async def refused(request: ChatRequest, item: Interaction) -> None:
            raise GatewayError(
                "cost_budget", 429, "Reservation 129453 microEUR; limit 21273 microEUR"
            )

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch("services.orchestrator.chat_api.process", refused),
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "Explain"}]},
            )
        self.assertEqual(response.status_code, 429)
        self.assertIn("129453", response.text)


class ProviderFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_failure_has_one_retry_and_finish_reason(self) -> None:
        from collections.abc import AsyncGenerator
        from agent_provider import AgentProvider, AgentRequest, Usage
        from provider_retry import TransientProviderError
        from packages.limits import ProviderFailure
        from quality import stream_quality
        from serverless_support import environment
        from test_quality_gateway import request

        calls = 0

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            raise TransientProviderError(
                "empty_completion",
                usage=Usage(prompt_tokens=20, completion_tokens=1),
                finish_reason="stop",
            )
            yield {}

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            with self.assertRaises(ProviderFailure) as caught:
                _ = [
                    event
                    async for event in stream_quality(
                        AgentProvider(), request(max_tokens=100)
                    )
                ]
        self.assertEqual(calls, 2)
        self.assertEqual(caught.exception.retries, 1)
        self.assertIn("finish_reason=stop", caught.exception.detail)


class AttachmentTests(unittest.TestCase):
    def test_file_requests_have_bounded_deadline_without_attachment(self) -> None:
        from agent_provider import AgentRequest
        from pydantic import ValidationError

        for question in (
            "Crée un PPTX",
            "PREPARE UN PDF",
            "genere un docx",
            "Build a spreadsheet",
            "Erstelle eine Präsentation",
            "Convertis ceci en XLSX",
        ):
            with self.subTest(question=question):
                payload = ChatRequest(
                    messages=[ChatMessage(role="user", content=question)]
                )
                self.assertFalse(payload.has_attachments)
                self.assertEqual(payload.timeout_seconds, 300)
        Limits(profile=PROFILES[0], produces_files=True, wall_clock=300)
        data = dict(
            profile=PROFILES[0],
            produces_files=True,
            messages=[{"role": "user", "content": "Create a PDF"}],
            tools=[],
            timeout=300.0,
        )
        AgentRequest.model_validate(data)
        with self.assertRaises(ValidationError):
            AgentRequest.model_validate({**data, "timeout": 301.0})
        with self.assertRaises(ValueError):
            Limits(profile=PROFILES[0], produces_files=True, wall_clock=301)

    def test_filesystem_attachment_phrasings_keep_their_budget(self) -> None:
        for question in (
            "Résume ces deux pièces",
            "Dégage les conclusions",
            "RESUME STRUCTURE",
            "resume ces pieces",
            "Summarize these attachments",
            "Fasse die Beilagen zusammen",
        ):
            payload = ChatRequest(
                messages=[
                    ChatMessage(
                        role="user",
                        content='<attached_files>\n<file type="filesystem" id="a" url="/home/user/a.pdf"/>'
                        '<file type="filesystem" id="b" url="/home/user/b.pdf"/>\n</attached_files>\n'
                        + question,
                    )
                ]
            )
            with self.subTest(question=question):
                self.assertTrue(payload.has_attachments)
                self.assertEqual(payload.timeout_seconds, 300)
        plain = ChatRequest(
            messages=[
                ChatMessage(role="user", content="No attached files; compute 2+2")
            ]
        )
        self.assertFalse(plain.has_attachments)
        self.assertEqual(plain.timeout_seconds, 120)
