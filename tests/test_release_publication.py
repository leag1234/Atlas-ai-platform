"""REQ-PROJ-05: publication uses the remote source revision, never scratch git."""

import json
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from services.orchestrator.code_verification import Verification
from services.orchestrator.work_forge_tools import execute


class PublicationRevisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_scratch_sha_is_replaced_before_freezing_publication(self) -> None:
        tools = MagicMock()
        tools.verification = Verification()
        tools.verification.observe(
            "run_command",
            {"command": "python3 -m unittest -v"},
            {"exit_code": 0, "workspace_changed": False},
        )
        tools.execution.session = {"repository_id": "repo"}
        tools.execution.broker.public = AsyncMock(
            return_value=(
                200,
                {
                    "items": [
                        {"id": "repo", "folder_id": "folder", "base_commit": "b" * 40}
                    ]
                },
            )
        )
        tools.workspace.metadata.return_value = {"baseline": "a" * 40}
        tools.workspace.invoke = AsyncMock(return_value={"error": "stop-before-export"})
        # Observe the bound action without allowing any publication side effect.
        from unittest.mock import patch
        from services.orchestrator.work_forge_tools import ForgeRequest

        original = ForgeRequest.action
        actions: list[dict[str, Any]] = []

        def action(request: ForgeRequest) -> dict[str, Any]:
            value = original(request)
            actions.append(value)
            return value

        with patch.object(ForgeRequest, "action", action):
            with self.assertRaisesRegex(ValueError, "repository_export_failed"):
                await execute(
                    tools,
                    json.dumps(
                        dict(
                            kind="publish_change",
                            base_commit="a" * 40,
                            branch="review/average",
                            target="main",
                            message="Average",
                            title="Average",
                            body="Five tests passed",
                        )
                    ),
                    5,
                )
        self.assertEqual(actions[0]["base_commit"], "b" * 40)
        tools.execution.broker.call.assert_not_called()

    def test_masked_shell_exit_remains_unverified(self) -> None:
        verification = Verification()
        verification.observe(
            "run_command",
            {"command": 'python3 -m unittest -v; echo "exit=$?"'},
            {"exit_code": 0, "workspace_changed": False},
        )
        self.assertEqual(verification.status().status, "not_run")
        verification.observe(
            "run_command",
            {"command": "python3 -m unittest -v"},
            {"exit_code": 0, "workspace_changed": False},
        )
        self.assertEqual(verification.status().status, "passed")

    def test_stderr_redirect_preserves_real_test_success_and_failure(self) -> None:
        import shlex
        import subprocess
        import sys
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            test = Path(directory) / "test_example.py"
            command = f"cd {shlex.quote(directory)} && {shlex.quote(sys.executable)} -m unittest -v 2>&1"
            for body, expected in [
                ("self.assertEqual(1, 1)", "passed"),
                ("self.fail('failure')", "failed"),
            ]:
                test.write_text(
                    "import unittest\nclass Example(unittest.TestCase):\n def test_example(self):\n  "
                    + body
                    + "\n"
                )
                result = subprocess.run(
                    command, shell=True, executable="/bin/bash", capture_output=True
                )
                verification = Verification()
                verification.observe(
                    "run_command",
                    {"command": command},
                    {"exit_code": result.returncode, "workspace_changed": False},
                )
                self.assertEqual(verification.status().status, expected)
            for masked in [
                command + "; true",
                command + " || true",
                "python3 -m unittest; echo ok 2>&1",
                "python3 -m unittest | cat 2>&1",
            ]:
                verification = Verification()
                verification.observe(
                    "run_command",
                    {"command": masked},
                    {"exit_code": 0, "workspace_changed": False},
                )
                self.assertEqual(verification.status().status, "not_run")
