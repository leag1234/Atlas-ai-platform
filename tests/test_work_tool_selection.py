"""Work group selection and live next-step switching, without provider calls."""

import json
import asyncio
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from services.orchestrator.code_mode import CodeModel
from services.orchestrator.code_tools import WorkTools
from services.orchestrator.loop import Call
from services.orchestrator.model import Configuration
from services.orchestrator.work_tool_selection import (
    CORE,
    GROUPS,
    WorkToolSelection,
    select_group,
)


class WorkSelectionTests(unittest.IsolatedAsyncioTestCase):
    def test_every_group_obeys_existing_gateway_schema(self) -> None:
        from agent_provider import AgentRequest

        model = CodeModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal("0.5"),
                output_eur_per_mtok=Decimal("1"),
                max_tokens=2048,
            ),
        )
        for group in GROUPS:
            selection = WorkToolSelection(group)
            tools = selection.declarations()
            self.assertLessEqual(len(tools), 16)
            names = [f["name"] for t in tools if isinstance(f := t["function"], dict)]
            self.assertEqual(names[:3], list(CORE))
            self.assertEqual(len(names), len(set(names)))
            AgentRequest.model_validate(
                dict(
                    messages=[dict(role="user", content="Task")],
                    tools=tools,
                    profile=model.profile,
                    timeout=30.0,
                )
            )

    def test_project_code_group_offers_every_code_tool(self) -> None:
        selection = WorkToolSelection("code")
        selection.reopen_enabled = True
        selection.memory_enabled = True
        selection.schedules_enabled = True
        from services.orchestrator.schedule_tools import ScheduleTools

        selection.catalog["propose_scheduled_task"] = ScheduleTools(
            None,
            {},
            "UTC",
            "en",
        ).declaration()
        declarations = selection.declarations()
        names = [
            f["name"] for t in declarations if isinstance(f := t["function"], dict)
        ]
        self.assertEqual(names[:3], list(CORE))
        for name in (
            "reopen_result",
            "project_memory",
            "propose_scheduled_task",
            "edit_file",
            "run_command",
            "remote_run",
            "remote_status",
            "read_file",
            "write_file",
            "search",
        ):
            self.assertIn(name, names)
        self.assertLessEqual(len(names), 16)

    def test_research_group_lists_web_tools_and_stays_within_capacity(self) -> None:
        selection = WorkToolSelection("research")
        self.assertEqual(selection.names, (*CORE, "web_search", "web_fetch"))
        declarations = selection.declarations()
        names = [
            f["name"] for t in declarations if isinstance(f := t["function"], dict)
        ]
        self.assertEqual(names, [*CORE, "web_search", "web_fetch"])
        self.assertLessEqual(len(names), 8)
        switch = selection.catalog["switch_tool_group"]["function"]
        assert isinstance(switch, dict)
        self.assertIn("research", switch["parameters"]["properties"]["group"]["enum"])

    def test_research_is_offered_only_once_enabled(self) -> None:
        selection = WorkToolSelection("files")
        switch = selection.catalog["switch_tool_group"]["function"]
        assert isinstance(switch, dict)
        self.assertNotIn(
            "research", switch["parameters"]["properties"]["group"]["enum"]
        )
        self.assertNotIn("research (", switch["description"])
        selection.enable_research()
        self.assertIn("research", switch["parameters"]["properties"]["group"]["enum"])
        self.assertIn("research (web search/fetch)", switch["description"])

    def test_strict_privacy_removes_research_and_refuses_direct_web_calls(self) -> None:
        selection = WorkToolSelection("research")
        selection.disable_research()
        self.assertNotIn("research", selection.groups)
        self.assertEqual(selection.group, "files")
        switch = selection.catalog["switch_tool_group"]["function"]
        assert isinstance(switch, dict)
        self.assertNotIn(
            "research", switch["parameters"]["properties"]["group"]["enum"]
        )

        execution = MagicMock()
        execution.check = AsyncMock()
        execution.broker.call = AsyncMock()
        tools = WorkTools(MagicMock(), execution)
        tools.selection = selection
        with patch.object(tools, "emit", new=AsyncMock()) as emit:
            result = asyncio.run(
                tools.execute(Call("fetch", "web_fetch", '{"url":"https://a.test"}'), 1)
            )
        self.assertEqual(
            result,
            {
                "error": "tool_not_selected",
                "group": "files",
                "next_step": "Use switch_tool_group to select the required tools",
            },
        )
        emit.assert_not_awaited()
        execution.broker.call.assert_not_awaited()

    def test_deterministic_request_priority_and_content_fallback(self) -> None:
        for text, expected in (
            ("WRITE THE MEMO", "documents"),
            ("Prepare slides", "presentations"),
            ("Répare le code", "code"),
            ("Export XLSX", "spreadsheets"),
            ("Déplace les fichiers", "files"),
            ("Search the web for the latest fixture", "research"),
            ("Make DOCX from XLSX", "spreadsheets"),
        ):
            self.assertEqual(select_group(text, []), expected)
        for suffix, group in (
            ("xlsx", "spreadsheets"),
            ("csv", "spreadsheets"),
            ("py", "code"),
            ("pptx", "presentations"),
            ("md", "documents"),
        ):
            files = [
                dict(relative_path="source." + suffix),
                dict(relative_path="unknown.bin"),
            ]
            self.assertEqual(select_group("Continue", files), group)
            self.assertEqual(select_group("Continue", list(reversed(files))), group)
        self.assertEqual(select_group("Continue", [], True), "code")
        self.assertEqual(select_group("Continue", []), "files")

    def test_file_name_extensions_do_not_select_documents(self) -> None:
        for text in (
            "Fix the bug in parser.py and update README.md",
            "Fix the failing test; the expected output is in notes.txt",
        ):
            self.assertEqual(select_group(text, [], True), "code")
        self.assertEqual(
            select_group("Fix the bug in parser.py and update README.md", []), "code"
        )
        self.assertEqual(
            select_group(
                "Fix the failing test; the expected output is in notes.txt", [], True
            ),
            "code",
        )
        for text in (
            "Write a markdown summary of the meeting",
            "Fais-moi un fichier md avec le compte rendu",
            "Give me the result as a txt file",
        ):
            self.assertEqual(select_group(text, []), "documents")
        self.assertEqual(
            select_group("Organize my .txt files into folders", []), "files"
        )
        self.assertEqual(select_group("Update README.md", [], True), "code")

    def test_code_context_wins_over_incidental_work_keywords(self) -> None:
        python_file = dict(relative_path="worker.py")
        self.assertEqual(
            select_group("Explain the report command", [python_file]), "code"
        )
        self.assertEqual(select_group("Fix the CSV export in exporter.py", []), "code")
        self.assertEqual(
            select_group("Make a pptx of the results", [python_file]),
            "presentations",
        )

    async def test_switch_changes_next_model_step_without_granting_authority(
        self,
    ) -> None:
        model = CodeModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal("0.5"),
                output_eur_per_mtok=Decimal("1"),
                max_tokens=2048,
            ),
        )
        execution = MagicMock()
        execution.check = AsyncMock()
        workspace = MagicMock()
        tools = WorkTools(workspace, execution)
        emitter = patch.object(tools, "emit", new=AsyncMock())
        emitter.start()
        self.addCleanup(emitter.stop)
        tools.selection = WorkToolSelection("documents")
        model.work_tools = tools.selection.declarations
        before = model.available_tools([])
        result = await tools.execute(
            Call("switch", "switch_tool_group", json.dumps(dict(group="code"))), 10
        )
        self.assertEqual(tools.selection.group, "documents")
        self.assertIn("publish_document", tools.selection.names)
        after = model.available_tools([])
        self.assertEqual(result["group"], "code")
        self.assertNotEqual(before, after)
        self.assertIn("edit_file", str(after))
        self.assertNotIn("publish_document", str(after))
        execution.check.assert_awaited_once()
        invalid = await tools.execute(
            Call("bad", "switch_tool_group", '{"group":"everything"}'), 10
        )
        self.assertEqual(invalid["error"], "invalid_tool_group")
        self.assertEqual(model.available_tools([]), after)

        unavailable = await tools.execute(Call("outside", "publish_document", "{}"), 10)
        self.assertEqual(unavailable["error"], "tool_not_selected")
        workspace.invoke.assert_not_called()

    async def test_invalid_approval_is_recoverable_without_issuing_permission(
        self,
    ) -> None:
        execution = MagicMock()
        execution.check = AsyncMock()
        execution.broker.call = AsyncMock()
        tools = WorkTools(MagicMock(), execution)
        tools.selection = WorkToolSelection("documents")
        with patch.object(tools, "emit", new=AsyncMock()):
            result = await tools.execute(
                Call(
                    "approval",
                    "request_approval",
                    json.dumps(
                        {
                            "action_kind": "delete",
                            "effects": "invalid synthetic input",
                            "maximum_bytes": 97,
                        }
                    ),
                ),
                10,
            )
        self.assertEqual(result["error"], "invalid_tool_arguments")
        self.assertIn("model_type", str(result["details"]))
        self.assertNotIn("invalid synthetic input", str(result))
        execution.broker.call.assert_not_awaited()
        self.assertEqual(tools.selection.group, "documents")
