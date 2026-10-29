"""Schedule transport denies API keys and foreign origins before broker access."""

import unittest
from unittest.mock import patch, AsyncMock

from fastapi.testclient import TestClient
from services.orchestrator.chat_api import app


class ScheduleSecurityTests(unittest.TestCase):
    def test_api_key_and_origin_denied_before_identity_resolution(self) -> None:
        path = "/api/atlas/orgs/10000000-0000-4000-8000-000000000001/projects/20000000-0000-4000-8000-000000000001/scheduled-tasks"
        with (
            patch.dict(
                "os.environ",
                {
                    "ATLAS_PROJECT_MODE": "1",
                    "ATLAS_PROJECT_ORIGIN": "http://testserver",
                },
            ),
            patch(
                "services.orchestrator.project_ui.broker_identity", AsyncMock()
            ) as identity,
        ):
            with TestClient(app) as client:
                for suffix in ("", "/60000000-0000-4000-8000-000000000001/run"):
                    response = client.post(
                        path + suffix,
                        json={},
                        headers={
                            "Origin": "http://testserver",
                            "Authorization": "Bearer fictional-api-key",
                        },
                    )
                    self.assertEqual(response.status_code, 403)
                response = client.post(
                    path, json={}, headers={"Origin": "https://foreign.example.test"}
                )
                self.assertEqual(response.status_code, 403)
                identity.assert_not_called()

    def test_project_chat_boundary_accepts_owner_timezone(self) -> None:
        import asyncio
        from uuid import uuid4
        from starlette.requests import Request
        from services.orchestrator.project_transcript import authenticate_chat
        from services.orchestrator.chat_schema import ChatRequest

        body = dict(
            model=ChatRequest.model_fields["model"].default,
            org_id=str(uuid4()),
            project_id=str(uuid4()),
            conversation_id=str(uuid4()),
            messages=[
                dict(
                    role="user", content="Refresh the fictional orchard summary daily."
                )
            ],
            stream=True,
            ui_locale="fr",
            ui_timezone="Europe/Paris",
        )
        request = Request(
            dict(
                type="http",
                method="POST",
                path="/v1/chat/completions",
                headers=[(b"idempotency-key", b"fictional-schedule-request")],
            )
        )
        with (
            patch("services.orchestrator.project_transcript.browser_origin"),
            patch(
                "services.orchestrator.project_transcript.verified_subject",
                AsyncMock(return_value="editor"),
            ),
            patch(
                "services.orchestrator.project_transcript.broker_identity", AsyncMock()
            ),
        ):
            subject, key = asyncio.run(authenticate_chat(request, body))
        self.assertEqual(subject, "editor")
        self.assertEqual(key, "fictional-schedule-request")
        self.assertEqual(ChatRequest.model_validate(body).ui_timezone, "Europe/Paris")
