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
                self.assertLessEqual(len(tools), 16)
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


class SearchLanguageDefaultTests(unittest.TestCase):
    def test_a_search_without_language_takes_the_conversation_language(self) -> None:
        from services.orchestrator.code_tools import with_search_language
        from services.orchestrator.loop import Call

        bare = Call("c1", "web_search", json.dumps({"query": "fibre offers"}))
        self.assertEqual(
            json.loads(with_search_language(bare, "fr").arguments),
            {"query": "fibre offers", "lang": "fr"},
        )
        named = Call("c2", "web_search", json.dumps({"query": "x", "lang": "de"}))
        self.assertIs(with_search_language(named, "fr"), named)
        fetch = Call("c3", "web_fetch", json.dumps({"url": "https://example.org"}))
        self.assertIs(with_search_language(fetch, "fr"), fetch)


class FreeNameTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_new_file_takes_the_next_free_name(self) -> None:
        from pathlib import PurePosixPath

        from services.orchestrator.project_document_tools import publish_with_free_name
        from services.retrieval.project_identity import ProjectError

        taken = {"club.pptx", "club-2.pptx"}
        asked: list[str] = []

        async def publish(name: str) -> dict[str, str]:
            asked.append(name)
            if name in taken:
                raise ProjectError(409, "path_conflict")
            return {"name": name}

        receipt = await publish_with_free_name(
            publish, PurePosixPath("club.pptx"), "created"
        )
        self.assertEqual(receipt, {"name": "club-3.pptx"})
        self.assertEqual(asked, ["club.pptx", "club-2.pptx", "club-3.pptx"])
        with self.assertRaises(ProjectError):
            await publish_with_free_name(
                publish, PurePosixPath("club.pptx"), "regenerated"
            )


class UnattendedApprovalTests(unittest.IsolatedAsyncioTestCase):
    def execution(self, decision_status: int = 200) -> tuple[Any, list[Any]]:
        calls: list[Any] = []

        class Broker:
            async def public(self, method: str, tail: str, **kwargs: Any) -> Any:
                calls.append((method, tail, kwargs))
                if method == "GET":
                    return 200, {
                        "pending_approvals": [{"id": "a1", "action_digest": "d1"}]
                    }
                return decision_status, {}

        class Execution:
            broker = Broker()
            session = {"id": "s1"}

        return Execution(), calls

    async def test_the_overwrite_is_accepted_as_a_click_would(self) -> None:
        from services.orchestrator.work_save import approve_unattended

        execution, calls = self.execution()
        self.assertTrue(await approve_unattended(execution, {"id": "a1"}))
        method, tail, sent = calls[-1]
        self.assertEqual((method, tail), ("POST", "approvals/a1/decision"))
        self.assertEqual(
            json.loads(sent["body"]), {"allow": True, "action_digest": "d1"}
        )
        self.assertIn("Idempotency-Key", sent["headers"])

    async def test_a_refused_decision_is_reported(self) -> None:
        from services.orchestrator.work_save import approve_unattended

        execution, _ = self.execution(410)
        self.assertFalse(await approve_unattended(execution, {"id": "a1"}))


class SettledSwitchRefusalTests(unittest.TestCase):
    def test_refused_switch_is_settled_once_a_file_is_published(self) -> None:
        from services.orchestrator.code_mode import open_failures

        refused = {"switch_tool_group": "deliverable_group_required"}
        self.assertEqual(open_failures(refused, []), refused)
        self.assertEqual(open_failures(refused, [{"deliverable_id": "d1"}]), {})
        self.assertEqual(open_failures({"read_file": "File must be regular"}, []), {})
        other = {"switch_tool_group": "invalid_tool_group", "run_command": "denied"}
        self.assertEqual(open_failures(other, [{"deliverable_id": "d1"}]), other)


class DeliverableGroupGuardTests(unittest.TestCase):
    def selection(self, pending: bool) -> WorkToolSelection:
        selection = WorkToolSelection("documents")
        selection.deliverable_pending = lambda: pending
        return selection

    def test_switch_away_from_publication_is_refused_until_the_file_exists(
        self,
    ) -> None:
        from services.orchestrator.work_tool_selection import ToolSelectionError

        selection = self.selection(True)
        with self.assertRaises(ToolSelectionError) as refused:
            selection.switch(json.dumps({"group": "files"}))
        self.assertEqual(
            refused.exception.result["error"], "deliverable_group_required"
        )
        self.assertIn("publish_document", refused.exception.result["next_step"])
        self.assertEqual(selection.group, "documents")
        self.assertIsNone(selection.pending)

    def test_one_research_visit_is_allowed_before_the_file(self) -> None:
        from services.orchestrator.work_tool_selection import ToolSelectionError

        selection = self.selection(True)
        selection.enable_research()
        first = selection.switch(json.dumps({"group": "research"}))
        self.assertEqual(first["group"], "research")
        self.assertIn("publish_document", first["next_step"])
        selection.declarations()
        self.assertEqual(
            selection.switch(json.dumps({"group": "documents"}))["group"], "documents"
        )
        selection.declarations()
        # A second visit, or research never enabled, still waits for the file.
        with self.assertRaises(ToolSelectionError):
            selection.switch(json.dumps({"group": "research"}))
        offline = self.selection(True)
        offline.disable_research()
        with self.assertRaises((ToolSelectionError, ValueError)):
            offline.switch(json.dumps({"group": "research"}))

    def test_a_research_visit_returns_to_the_publishing_group(self) -> None:
        from services.orchestrator.work_tool_selection import ToolSelectionError

        selection = WorkToolSelection("spreadsheets")
        selection.deliverable_pending = lambda: True
        selection.enable_research()
        selection.switch(json.dumps({"group": "research"}))
        selection.declarations()
        for target in ("files", "code", "research"):
            with self.subTest(target=target):
                with self.assertRaises(ToolSelectionError) as refused:
                    selection.switch(json.dumps({"group": target}))
                result = refused.exception.result
                self.assertEqual(result["error"], "deliverable_group_required")
                self.assertEqual(result["group"], "spreadsheets")
                self.assertIn(
                    "Switch back to the spreadsheets group", result["next_step"]
                )
                self.assertEqual(selection.group, "research")
        self.assertEqual(
            selection.switch(json.dumps({"group": "spreadsheets"}))["group"],
            "spreadsheets",
        )
        # Once the file exists the agent moves freely again.
        selection.declarations()
        done = WorkToolSelection("documents")
        done.deliverable_pending = lambda: False
        done.enable_research()
        done.switch(json.dumps({"group": "research"}))
        done.declarations()
        self.assertEqual(done.switch(json.dumps({"group": "files"}))["group"], "files")

    def test_research_as_the_first_group_keeps_its_free_switch(self) -> None:
        selection = WorkToolSelection("research")
        selection.deliverable_pending = lambda: True
        self.assertEqual(
            selection.switch(json.dumps({"group": "files"}))["group"], "files"
        )

    def test_switch_is_free_once_the_file_exists_or_none_was_requested(self) -> None:
        self.assertEqual(
            self.selection(False).switch(json.dumps({"group": "files"}))["group"],
            "files",
        )
        plain = WorkToolSelection("documents")
        self.assertEqual(plain.switch(json.dumps({"group": "files"}))["group"], "files")

    def test_switch_to_another_publishing_group_and_from_code_stay_allowed(
        self,
    ) -> None:
        self.assertEqual(
            self.selection(True).switch(json.dumps({"group": "spreadsheets"}))["group"],
            "spreadsheets",
        )
        code = WorkToolSelection("code")
        code.deliverable_pending = lambda: True
        self.assertEqual(code.switch(json.dumps({"group": "files"}))["group"], "files")

    def test_the_turn_decision_wins_over_the_lexical_file_check(self) -> None:
        from services.orchestrator.code_mode import code_deliverable_pending

        question = "Je voudrais un PDF qui résume les règles du jeu de Kestrel"
        # No creation verb: the lexical check alone misses this request.
        self.assertFalse(code_deliverable_pending(question, "documents", []))
        self.assertTrue(code_deliverable_pending(question, "documents", [], True))
        self.assertFalse(
            code_deliverable_pending(question, "documents", [{"id": "d1"}], True)
        )
        self.assertFalse(
            code_deliverable_pending("Create a docx report", "documents", [], False)
        )

    def test_code_deliverable_lock_applies_only_to_office_formats(self) -> None:
        from services.orchestrator.code_mode import code_deliverable_pending

        self.assertFalse(
            code_deliverable_pending(
                "Create a downloadable file containing the Python script",
                "code",
                [],
            )
        )
        self.assertTrue(
            code_deliverable_pending(
                "Create a downloadable file containing the Python script",
                "documents",
                [],
            )
        )
        self.assertTrue(
            code_deliverable_pending(
                "Create a docx report from parser.py",
                "code",
                [],
            )
        )
