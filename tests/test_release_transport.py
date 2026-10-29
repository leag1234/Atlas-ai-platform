"""Release 0.7: the native project composer satisfies scoped authentication."""

import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from starlette.requests import Request

from packages.profiles import PROFILES
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.project_transcript import authenticate_chat
from services.retrieval.project_identity import ProjectError


class ProjectTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_ui_marker_crosses_the_closed_project_boundary(self) -> None:
        raw = dict(
            atlas_ui=True,
            model=PROFILES[0],
            ui_locale="fr",
            org_id=str(uuid4()),
            project_id=str(uuid4()),
            conversation_id=str(uuid4()),
            work_session_id=str(uuid4()),
            project_action="work",
            file_version_ids=[],
            messages=[dict(role="user", content="Prépare le document.")],
            stream=True,
        )
        request = Request(
            dict(
                type="http",
                method="POST",
                path="/v1/chat/completions",
                scheme="http",
                server=("testserver", 80),
                headers=[
                    (b"host", b"testserver"),
                    (b"origin", b"http://testserver"),
                    (b"idempotency-key", b"release-ui"),
                ],
            )
        )
        with (
            patch(
                "services.orchestrator.project_transcript.verified_subject",
                AsyncMock(return_value="ordinary-member"),
            ) as subject,
            patch(
                "services.orchestrator.project_transcript.broker_identity", AsyncMock()
            ) as identity,
        ):
            self.assertEqual(
                await authenticate_chat(request, raw),
                ("ordinary-member", "release-ui"),
            )
            subject.assert_awaited_once()
            assert identity.await_args is not None
            self.assertEqual(
                identity.await_args.args[1:],
                ("ordinary-member", raw["org_id"], raw["project_id"]),
            )
            self.assertTrue(ChatRequest.model_validate(raw).atlas_ui)
            for invalid in (dict(raw, atlas_ui="true"), dict(raw, bypass_scope=True)):
                with self.subTest(invalid=invalid), self.assertRaises(ProjectError):
                    await authenticate_chat(request, invalid)
            self.assertEqual(identity.await_count, 1)
