"""Project scope dispatch precedes every generic chat route."""

import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from typing import Any, Literal
from unittest.mock import ANY, AsyncMock, patch

from decimal import Decimal
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from services.orchestrator.chat_api import app
from services.orchestrator.chat_pipeline import dispatch_project, process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Turn
from services.orchestrator.model import Configuration, GatewayModel


PROJECT = "00000000-0000-0000-0000-000000000001"
CONVERSATION = "00000000-0000-0000-0000-000000000002"
SESSION = "00000000-0000-0000-0000-000000000003"


class ProjectDispatchTests(unittest.IsolatedAsyncioTestCase):
    def request(self, **updates: object) -> ChatRequest:
        return ChatRequest.model_validate(
            {
                "project_id": PROJECT,
                "conversation_id": CONVERSATION,
                "messages": [{"role": "user", "content": "Review this."}],
                **updates,
            }
        )

    async def test_project_dispatch_precedes_attachment_and_vision_routes(self) -> None:
        image = base64.b64encode(
            Path("tests/cassettes/vision/shapes.png").read_bytes()
        ).decode()
        request = self.request(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Review this."},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + image},
                        },
                    ],
                }
            ]
        )
        routed = AsyncMock()
        with (
            patch("services.orchestrator.project_chat.process_project", routed),
            patch(
                "services.orchestrator.document_chat.process_documents", AsyncMock()
            ) as attachments,
            patch("services.orchestrator.vision.process_vision", AsyncMock()) as vision,
        ):
            await process(request, Interaction())
        routed.assert_awaited_once()
        attachments.assert_not_awaited()
        vision.assert_not_awaited()

    async def test_work_is_explicit_and_chat_is_the_default(self) -> None:
        chat = self.request()
        work = self.request(project_action="work", work_session_id=SESSION)
        routed = AsyncMock()
        with patch("services.orchestrator.project_chat.process_project", routed):
            self.assertTrue(await dispatch_project(chat, Interaction()))
            self.assertTrue(await dispatch_project(work, Interaction()))
        self.assertEqual(routed.await_args_list[0].args[0].project_action, "chat")
        self.assertEqual(routed.await_args_list[1].args[0].work_session_id, SESSION)

    async def test_continuation_in_project_skips_selector(self) -> None:
        request = self.request(continuation=True)
        routed = AsyncMock()
        with (
            patch(
                "services.orchestrator.project_commands.select", AsyncMock()
            ) as select,
            patch("services.orchestrator.project_chat.process_project", routed),
        ):
            self.assertTrue(await dispatch_project(request, Interaction()))
        select.assert_not_awaited()
        routed.assert_awaited_once_with(request, ANY)

    def test_work_needs_a_scoped_session_and_conflicts_are_rejected(self) -> None:
        base = {
            "project_id": PROJECT,
            "conversation_id": CONVERSATION,
            "messages": [{"role": "user", "content": "Review this."}],
        }
        for update in (
            {"project_action": "work"},
            {"work_session_id": SESSION},
            {"code_mode": True, "code_session_id": SESSION},
        ):
            with (
                self.subTest(update=update),
                self.assertRaisesRegex(
                    ValueError, "selector_conflict|work_session_required"
                ),
            ):
                ChatRequest.model_validate({**base, **update})

    def test_legacy_code_selector_remains_available_without_project_scope(self) -> None:
        with patch("services.orchestrator.code_workspace.enabled", return_value=True):
            request = ChatRequest.model_validate(
                {
                    "code_mode": True,
                    "code_session_id": SESSION,
                    "messages": [{"role": "user", "content": "Review this."}],
                }
            )
        self.assertTrue(request.code_mode)


class ProjectDocumentDispatchTests(unittest.TestCase):
    def test_project_pdf_uses_document_reader_with_scoped_history(self) -> None:
        canary = "FOREIGN_PROJECT_HISTORY_CANARY"
        pdf = self.pdf("DOCUMENT_READER_CANARY")
        captured: list[list[dict[str, object]]] = []
        requests: list[tuple[str, dict[str, Any]]] = []
        model = GatewayModel(
            "http://gateway",
            Configuration(
                input_eur_per_mtok=Decimal("0.5"),
                output_eur_per_mtok=Decimal("1"),
                max_tokens=2048,
            ),
        )

        async def complete(messages: list[dict[str, object]], timeout: float) -> Turn:
            captured.append(messages)
            return Turn("The PDF was read.")

        async def post(
            url: str, payload: dict[str, Any], timeout: float
        ) -> dict[str, Any]:
            requests.append((url, payload))
            if url.endswith("/context"):
                return {
                    "instructions": "",
                    "revision": 3,
                    "facts": [],
                    "history": [{"question": "Safe turn", "answer": "Safe answer"}],
                }
            return {}

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch.object(GatewayModel, "post", side_effect=post),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(model, "complete", AsyncMock(side_effect=complete)),
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "project_id": PROJECT,
                    "conversation_id": CONVERSATION,
                    "messages": [
                        {"role": "user", "content": canary},
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Summarize the PDF."},
                                {
                                    "type": "file",
                                    "file": {
                                        "filename": "brief.pdf",
                                        "file_data": base64.b64encode(pdf).decode(),
                                    },
                                },
                            ],
                        },
                    ],
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["choices"][0]["message"]["content"],
            "The PDF was read.",
        )
        self.assertEqual(len(captured), 1)
        self.assertNotIn(canary, json.dumps(requests[0][1]))
        self.assertTrue(requests[1][0].endswith(f"/projects/{PROJECT}/turns"))
        self.assertEqual(requests[1][1]["conversation_id"], CONVERSATION)
        self.assertEqual(requests[1][1]["answer"], "The PDF was read.")
        document_request = json.dumps(captured[0])
        self.assertIn("DOCUMENT_READER_CANARY", document_request)
        self.assertIn("Safe turn", document_request)
        self.assertNotIn(canary, document_request)

    @staticmethod
    def pdf(text: str) -> bytes:
        writer = PdfWriter()
        font = writer._add_object(
            DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
        )
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 10 Tf 50 740 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()


class ScopedProjectDispatchTests(unittest.IsolatedAsyncioTestCase):
    def state(self, action: str = "chat") -> dict[str, Any]:
        from uuid import uuid4

        root = Path("contracts/examples/project-mode")
        value: dict[str, Any] = json.loads(
            (root / "TranscriptState.valid.json").read_text()
        )
        value["turn"].update(
            id=str(uuid4()),
            request_id=str(uuid4()),
            conversation_id=CONVERSATION,
            action=action,
            state="running",
            content_refs=[],
        )
        value["turn"]["id"] = value["turn"]["request_id"]
        value["turn"]["usage"]["reserved_eur"] = 0.1
        value["context"].update(
            instructions_text="Use the selected reports.", facts=[], history=[]
        )
        return value

    def request(self, action: Literal["chat", "work"] = "chat") -> ChatRequest:
        value = ChatRequest(
            project_id=PROJECT,
            org_id=PROJECT,
            conversation_id=CONVERSATION,
            project_action=action,
            work_session_id=SESSION if action == "work" else None,
            messages=[
                ChatRequest.model_validate(
                    {"messages": [{"role": "user", "content": "Compare reports"}]}
                ).messages[0]
            ],
            stream=True,
        )
        value._project_subject, value._transcript_key = "editor", "request-key"
        return value

    def test_project_work_uses_work_deadline_including_cleanup(self) -> None:
        with (
            patch.dict(os.environ, {"ATLAS_CODE_WALL_SECONDS": "600"}),
            patch("services.orchestrator.code_workspace.enabled", return_value=True),
        ):
            self.assertEqual(self.request("work").timeout_seconds, 615)
            self.assertEqual(self.request("chat").timeout_seconds, 120)

    async def test_work_uses_authenticated_entry_and_canonical_context(self) -> None:
        from services.orchestrator.project_transcript import process_scoped_project

        state = self.state("work")
        calls = AsyncMock(side_effect=[state, state["turn"], dict(state, replay=True)])

        async def work(request: ChatRequest, item: Interaction, **kwargs: Any) -> None:
            self.assertEqual(
                kwargs,
                dict(
                    subject="editor",
                    org_id=PROJECT,
                    project_id=PROJECT,
                    session_id=SESSION,
                ),
            )
            self.assertIn("Use the selected reports.", request.messages[-2].text)
            item.state, item.reponse = (
                "done",
                "Reports compared; the second increases revenue.",
            )

        with (
            patch(
                "services.orchestrator.project_transcript.TranscriptBroker.call", calls
            ),
            patch(
                "services.orchestrator.code_mode.process_work", side_effect=work
            ) as authenticated,
            patch(
                "services.orchestrator.code_mode.process_code", AsyncMock()
            ) as legacy,
        ):
            await process_scoped_project(self.request("work"), Interaction())
        authenticated.assert_awaited_once()
        legacy.assert_not_awaited()
        self.assertEqual(calls.await_args_list[1].args[0], "finish")
        self.assertEqual(
            calls.await_args_list[1].args[1]["content_refs"][0]["text"],
            "Reports compared; the second increases revenue.",
        )

    async def test_work_without_organization_never_reaches_legacy_engine(self) -> None:
        from services.orchestrator.project_chat import process_project
        from services.retrieval.project_identity import ProjectError

        request = self.request("work")
        request.org_id = None
        with (
            patch(
                "services.orchestrator.code_mode.process_code", AsyncMock()
            ) as legacy,
            patch.object(GatewayModel, "post", AsyncMock()) as unscoped,
        ):
            with self.assertRaises(ProjectError) as failure:
                await process_project(request, Interaction())
        self.assertEqual(failure.exception.status, 401)
        legacy.assert_not_awaited()
        unscoped.assert_not_awaited()

    async def test_reload_emits_persisted_events_without_dispatch(self) -> None:
        from services.orchestrator.project_transcript import process_scoped_project
        from services.orchestrator.stream_client import sink_context

        state = self.state()
        state.update(replay=True, events=[{"seq": 8, "type": "recap"}])
        state["turn"]["content_refs"] = [
            dict(
                kind="answer",
                text="The approved report is ready.",
                file_version_id=None,
            )
        ]
        sink = AsyncMock()
        token = sink_context.set(sink)
        try:
            with (
                patch(
                    "services.orchestrator.project_transcript.TranscriptBroker.call",
                    AsyncMock(return_value=state),
                ) as broker,
                patch.object(GatewayModel, "connect", AsyncMock()) as model,
                patch(
                    "services.orchestrator.code_mode.process_work", AsyncMock()
                ) as work,
            ):
                item = Interaction()
                await process_scoped_project(self.request(), item)
            self.assertEqual(item.reponse, "The approved report is ready.")
            self.assertEqual(broker.await_count, 1)
            sink.assert_awaited_once_with({"project": {"seq": 8, "type": "recap"}})
            model.assert_not_awaited()
            work.assert_not_awaited()
        finally:
            sink_context.reset(token)

    async def test_org_and_versions_survive_validation_and_no_unscoped_fallback(
        self,
    ) -> None:
        from services.orchestrator.project_transcript import process_scoped_project

        request = self.request()
        request.file_version_ids = [SESSION]
        request._project_subject = None
        with patch.object(GatewayModel, "post", AsyncMock()) as old:
            with self.assertRaisesRegex(Exception, "unauthenticated"):
                await process_scoped_project(request, Interaction())
        old.assert_not_awaited()
        self.assertEqual(request.model_dump()["org_id"], PROJECT)
        self.assertEqual(request.model_dump()["file_version_ids"], [SESSION])

    async def test_legacy_alias_is_normalized_only_after_authentication(self) -> None:
        from starlette.requests import Request
        from services.orchestrator.project_transcript import authenticate_chat

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "scheme": "http",
                "server": ("testserver", 80),
                "headers": [
                    (b"host", b"testserver"),
                    (b"origin", b"http://testserver"),
                    (b"idempotency-key", b"legacy"),
                ],
            }
        )
        raw = dict(
            model=self.request().model,
            code_mode=True,
            code_session_id=SESSION,
            messages=[dict(role="user", content="Compare reports")],
            stream=True,
        )
        selection = dict(
            org_id=PROJECT,
            project_id=PROJECT,
            conversation_id=CONVERSATION,
            work_session_id=SESSION,
        )
        with (
            patch(
                "services.orchestrator.project_transcript.project_mode",
                return_value=True,
            ),
            patch(
                "services.orchestrator.project_transcript.verified_subject",
                AsyncMock(return_value="editor"),
            ),
            patch(
                "services.orchestrator.project_transcript.legacy_selection",
                AsyncMock(return_value=selection),
            ) as resolve,
            patch(
                "services.orchestrator.project_transcript.broker_identity",
                AsyncMock(return_value={}),
            ),
        ):
            subject, key = await authenticate_chat(request, raw)
        self.assertEqual((subject, key), ("editor", "legacy"))
        self.assertEqual(raw["project_action"], "work")
        self.assertNotIn("code_session_id", raw)
        assert resolve.await_args is not None
        self.assertEqual(resolve.await_args.args[1:], ("editor", SESSION))

    def test_scoped_conflicts_fail_before_upload_preparation(self) -> None:
        base = dict(
            model=self.request().model,
            org_id=PROJECT,
            project_id=PROJECT,
            conversation_id=CONVERSATION,
            messages=[dict(role="user", content="Review")],
            stream=True,
        )
        from services.retrieval.project_identity import ProjectError
        from services.orchestrator.project_transcript import authenticate_chat
        from starlette.requests import Request
        import asyncio

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [],
            }
        )
        for updates in (
            dict(code_session_id=SESSION),
            dict(work_session_id=SESSION),
            dict(org_id=None, file_version_ids=[SESSION]),
            dict(org_id=None, project_action="work", work_session_id=SESSION),
        ):
            with (
                self.subTest(updates=updates),
                self.assertRaises(ProjectError) as failure,
            ):
                asyncio.run(authenticate_chat(request, dict(base, **updates)))
            self.assertEqual(failure.exception.status, 409)
            self.assertEqual(failure.exception.body["error"], "selector_conflict")

    async def test_scope_failure_precedes_upload_preparation(self) -> None:
        payload = self.request().model_dump(
            include={
                "model",
                "org_id",
                "project_id",
                "conversation_id",
                "messages",
                "stream",
                "file_version_ids",
            }
        )
        # Frozen project messages contain only role and text.
        payload["messages"] = [{"role": "user", "content": "Compare reports"}]
        from services.retrieval.project_identity import ProjectError

        with (
            tempfile.TemporaryDirectory(dir="BRAIN") as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch(
                "services.orchestrator.project_transcript.verified_subject",
                AsyncMock(return_value="viewer"),
            ),
            patch(
                "services.orchestrator.project_transcript.broker_identity",
                AsyncMock(side_effect=ProjectError(404, "not_found")),
            ),
            patch("services.orchestrator.chat_api.prepare_uploads") as uploads,
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/chat/completions",
                json=payload,
                headers={"Origin": "http://testserver", "Idempotency-Key": "scope"},
            )
        self.assertEqual(response.status_code, 404, response.text)
        uploads.assert_not_called()
