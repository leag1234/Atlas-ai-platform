"""Work group selection and live next-step switching, without provider calls."""

import json
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
            self.assertLessEqual(len(tools), 8)
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

    def test_deterministic_request_priority_and_content_fallback(self) -> None:
        for text, expected in (
            ("WRITE THE MEMO", "documents"),
            ("Prepare slides", "presentations"),
            ("Répare le code", "code"),
            ("Export XLSX", "spreadsheets"),
            ("Déplace les fichiers", "files"),
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
