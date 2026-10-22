"""Project scope dispatch precedes every generic chat route."""

import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from typing import Any
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
