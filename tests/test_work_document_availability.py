"""REQ-PROJ-05: immutable document handles and publication reach live Work."""

import base64
import io
import json
import tempfile
from pathlib import Path

from openpyxl import Workbook
from pydantic import ValidationError

from decimal import Decimal
from typing import Any
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from services.orchestrator import code_worker, project_document_tools
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.code_mode import CodeModel, process_work
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import CodeResult
from services.orchestrator.work_tool_selection import WorkToolSelection, select_group


class DocumentAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_encoded_selector_reads_real_cells_and_preserves_validation(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        assert sheet is not None
        sheet.title = "Depenses"
        sheet.append(["Categorie", "EUR"])
        sheet.append(["Formation", 500])
        buffer = io.BytesIO()
        workbook.save(buffer)
        payload = dict(
            extension=".xlsx", content=base64.b64encode(buffer.getvalue()).decode()
        )
        selector = dict(sheet="Depenses", range="A1:B2")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            for value in (selector, json.dumps(selector)):
                parsed = project_document_tools.Read.model_validate(
                    dict(file_id="file", version="version", selector=value)
                )
                result = code_worker.perform(
                    Path(directory),
                    "read_document",
                    dict(payload, **parsed.model_dump(exclude_none=True)),
                )
                self.assertNotIn("error", result)
                self.assertIn("Formation", str(result["segments"]))
                self.assertIn("500", str(result["segments"]))
                self.assertTrue(result["coverage"]["complete"])
        for value in (
            "null",
            "[]",
            '"{}"',
            '{"page_start":true}',
            '{"unknown":1}',
            "{",
        ):
            with (
                self.subTest(invalid=value),
                self.assertRaises((ValidationError, ValueError)),
            ):
                project_document_tools.Read.model_validate(
                    dict(file_id="file", version="version", selector=value)
                )

    def test_document_publication_survives_memory_and_result_recall(self) -> None:
        for group in ("documents", "spreadsheets", "presentations"):
            with self.subTest(group=group):
                selection = WorkToolSelection(group)
                selection.memory_enabled = selection.reopen_enabled = True
                tools = selection.declarations()
                self.assertLessEqual(len(tools), 8)
                self.assertIn("read_document", selection.names)
                self.assertIn("publish_document", selection.names)
                self.assertIn("project_memory", selection.names)
                self.assertIn("reopen_result", selection.names)
                selection.switch('{"group":"documents","focus":"convert_document"}')
                selection.declarations()
                self.assertIn("convert_document", selection.names)

    def test_word_request_routes_to_documents_with_mixed_sources(self) -> None:
        files = [dict(relative_path="Budget.xlsx"), dict(relative_path="Brief.pdf")]
        for question in (
            "Rédige une note de synthèse Word à partir de ces documents",
            "Fais-moi une note Word",
            "Mets cette synthèse dans un document",
            "Prepare a Word briefing",
            "Schreibe einen Bericht",
            "Redacta un documento",
            "Une note Word",
            "Redige une note Word",
            "REDIGE UNE NOTE WORD",
            "Ne rédige pas de document",
        ):
            with self.subTest(question=question):
                self.assertEqual(select_group(question, files), "documents")

    async def test_live_work_context_carries_selected_document_handles(self) -> None:
        org, project, conversation, session_id, file_id, version, folder = (
            str(uuid4()) for _ in range(7)
        )
        file = dict(
            id=file_id,
            version_id=version,
            folder_id=folder,
            relative_path="Brief.pdf",
            mime="application/pdf",
        )
        session = dict(
            id=session_id,
            conversation_id=conversation,
            project_id=project,
            revision=0,
            effective_limits=dict(
                expanded_bytes=100,
                tool_calls=10,
                model_calls=11,
                wall_seconds=30,
                cost_eur=0.3,
                per_call_eur=0.1,
            ),
            base_file_versions=[dict(file_id=file_id, version_id=version)],
            environment={},
        )
        broker = MagicMock(org=org, project=project)
        broker.public = AsyncMock(
            side_effect=lambda method, path: (
                200,
                file if path.startswith("files/") else session,
            )
        )
        broker.call = AsyncMock(return_value=dict(session=session, events=[]))
        execution = MagicMock(session=session, broker=broker)
        execution.turn = AsyncMock()
        execution.check = AsyncMock()
        execution.snapshot = AsyncMock()
        workspace = MagicMock()
        workspace.prepare = AsyncMock()
        workspace.invoke = AsyncMock(return_value={"entries": [folder + "/"]})
        workspace.cleanup = AsyncMock()
        execution.workspace.return_value = workspace
        model = MagicMock(spec=CodeModel)
        model.spent = Decimal(0)
        model.input_tokens = model.output_tokens = 0
        model.work_unknown = False
        request = ChatRequest.model_validate(
            dict(
                messages=[dict(role="user", content="Prepare a Word briefing")],
                conversation_id=conversation,
                project_id=project,
            )
        )
        seen: list[dict[str, Any]] = []

        async def turn(*args: Any) -> CodeResult:
            seen.append(args[4])
            return CodeResult(text="Finished", state="done")

        with (
            patch("services.orchestrator.code_workspace.enabled", return_value=True),
            patch(
                "services.orchestrator.code_api.ExecutionBroker", return_value=broker
            ),
            patch(
                "services.orchestrator.code_mode.WorkExecution", return_value=execution
            ),
            patch.object(CodeModel, "connect", AsyncMock(return_value=model)),
            patch("services.orchestrator.code_mode.execute_turn", side_effect=turn),
        ):
            await process_work(
                request,
                Interaction(),
                subject="member",
                org_id=org,
                project_id=project,
                session_id=session_id,
            )
        self.assertEqual(len(seen), 1)
        self.assertEqual(
            seen[0].get("files"),
            [
                dict(
                    file_id=file_id,
                    version=version,
                    path=folder + "/Brief.pdf",
                    mime="application/pdf",
                )
            ],
        )
