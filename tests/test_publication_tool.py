"""Combined publication remains discoverable and returns the actual broker outcome."""

import base64
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from services.orchestrator.code_verification import Verification
from services.orchestrator.work_forge_tools import ForgeRequest, execute
from services.orchestrator.work_tool_selection import WorkToolSelection
from test_work_forge_tools import archive


class PublicationToolTests(unittest.IsolatedAsyncioTestCase):
    def test_memory_and_reopen_preserve_publication_within_eight_tools(self) -> None:
        selection = WorkToolSelection("code")
        selection.reopen_enabled = True
        selection.memory_enabled = True
        self.assertIn("forge_action", selection.names)
        self.assertIn("project_memory", selection.names)
        self.assertIn("reopen_result", selection.names)
        self.assertIn("edit_file", selection.names)
        self.assertIn("run_command", selection.names)
        self.assertEqual(len(selection.declarations()), 8)
        selection.switch(json.dumps(dict(group="code", focus="read_file")))
        self.assertIn("read_file", str(selection.declarations()))
        selection.switch(json.dumps(dict(group="code")))
        self.assertIn("forge_action", str(selection.declarations()))

    async def test_combined_snapshot_approval_and_result(self) -> None:
        args = dict(
            kind="publish_change",
            base_commit="a" * 40,
            branch="fix/repair",
            target="main",
            message="Repair",
            title="Repair total",
            body="Tests verified",
        )
        self.assertEqual(
            ForgeRequest.model_validate(args).action()["parameters"]["body"],
            "Tests verified",
        )
        tools = MagicMock()
        tools.verification = Verification()
        tools.verification.observe(
            "run_command",
            {"command": "pytest"},
            {"exit_code": 0, "workspace_changed": False},
        )
        tools.workspace.metadata.return_value = {"baseline": "a" * 40}
        tools.workspace.invoke = AsyncMock(
            return_value={"archive": base64.b64encode(archive()).decode()}
        )
        tools.emit = AsyncMock()
        execution = tools.execution
        execution.session = {
            "repository_id": "repository",
            "effective_limits": {"expanded_bytes": 100},
        }
        execution.fence.return_value = {"session_id": "session", "generation": 1}
        execution.check = AsyncMock()
        execution.broker.public = AsyncMock(
            return_value=(200, {"items": [{"id": "repository", "folder_id": "repo"}]})
        )
        pending = dict(operation_id="operation", approval=dict(decision="pending"))
        allowed = dict(
            operation_id="operation", approval=dict(decision="allowed"), state="pending"
        )
        complete = dict(
            allowed,
            state="succeeded",
            commit="b" * 40,
            pr_url="https://forge.example/org/repo/pulls/1",
            completed_steps=["branch", "commit", "pull_request"],
            failed_step=None,
        )
        execution.broker.call = AsyncMock(
            side_effect=[pending, allowed, allowed, complete]
        )
        result = await execute(tools, json.dumps(args), 5)
        self.assertEqual(result["pr_url"], complete["pr_url"])
        self.assertEqual(result["approval"]["decision"], "allowed")
        self.assertEqual(result["completed_steps"], complete["completed_steps"])
        self.assertEqual(result["state"], "succeeded")
        tools.workspace.invoke.assert_awaited_once()
        calls = execution.broker.call.await_args_list
        self.assertIn("snapshot_content", calls[0].args[1])
        self.assertEqual(calls[0].args[1]["action"]["kind"], "publish_change")
        self.assertNotIn("snapshot_content", calls[2].args[1])
        self.assertEqual(calls[2].args[1]["operation"], "execute")

    async def test_denial_returns_decision_and_never_executes(self) -> None:
        tools = MagicMock()
        tools.emit = AsyncMock()
        execution = tools.execution
        execution.session = {"repository_id": "repository"}
        execution.fence.return_value = {"session_id": "session", "generation": 1}
        execution.check = AsyncMock()
        execution.broker.public = AsyncMock(
            return_value=(200, {"items": [{"id": "repository", "folder_id": "repo"}]})
        )
        execution.broker.call = AsyncMock(
            side_effect=[
                dict(operation_id="operation", approval=dict(decision="pending")),
                dict(approval=dict(decision="denied")),
            ]
        )
        result = await execute(
            tools,
            json.dumps(dict(kind="branch", base_commit="a" * 40, branch="fix")),
            5,
        )
        self.assertEqual(result["error"], "approval_denied")
        self.assertEqual(result["approval"]["decision"], "denied")
        self.assertEqual(result["state"], "not_sent")
        self.assertEqual(execution.broker.call.await_count, 2)
