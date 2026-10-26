"""Work freezes exactly one snapshot and executes only its approved operation."""

import base64
import hashlib
import io
import json
import tarfile
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from services.orchestrator.project_forge import snapshot_files
from services.orchestrator.work_forge_tools import (
    ForgeRequest,
    execute,
    exported_snapshot,
)
from services.orchestrator.work_tool_selection import WorkToolSelection


def archive(link: bool = False) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as stream:
        for name in ("repo/answer.py", "outside/private.txt"):
            info = tarfile.TarInfo(name)
            info.mode = 0o755
            content = b"answer = 42\n"
            if link and name.startswith("repo/"):
                info.type = tarfile.SYMTYPE
                info.linkname = "../outside/private.txt"
                stream.addfile(info)
            else:
                info.size = len(content)
                stream.addfile(info, io.BytesIO(content))
    return output.getvalue()


class WorkForgeToolTests(unittest.IsolatedAsyncioTestCase):
    def test_snapshot_is_canonical_scoped_and_preserves_executable_mode(self) -> None:
        content = exported_snapshot(archive(), "repo", 100)
        files = snapshot_files(content, hashlib.sha256(content).hexdigest())
        self.assertEqual([f["path"] for f in files], ["answer.py"])
        self.assertEqual(files[0]["mode"], "100755")
        with self.assertRaisesRegex(ValueError, "unsafe_repository_export"):
            exported_snapshot(archive(link=True), "repo", 100)
        with self.assertRaisesRegex(ValueError, "repository_export_limit"):
            exported_snapshot(archive(), "repo", 1)

    def test_forge_is_discoverable_within_gateway_limit_and_arguments_are_exact(
        self,
    ) -> None:
        selection = WorkToolSelection("code")
        selection.reopen_enabled = True
        self.assertIn("forge_action", selection.names)
        self.assertLessEqual(len(selection.names), 8)
        args = ForgeRequest(kind="branch", base_commit="a" * 40, branch="fix")
        self.assertEqual(args.action()["parameters"], {"name": "fix"})
        with self.assertRaises(ValueError):
            ForgeRequest(
                kind="branch", base_commit="a" * 40, branch="fix", message="extra"
            ).action()

    async def test_export_once_before_approval_and_never_resend_mutable_bytes(
        self,
    ) -> None:
        tools = MagicMock()
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
        calls: list[dict[str, Any]] = []

        async def broker_call(route: str, data: dict[str, Any]) -> dict[str, Any]:
            self.assertEqual(route, "forge")
            calls.append(data)
            # Once approval is issued the workspace may change. No second
            # export or replacement payload may reach execution.
            if data["operation"] == "issue":
                tools.workspace.invoke.side_effect = AssertionError("second export")
            return {
                "operation_id": "operation",
                "approval": {"decision": "allowed"},
                "state": "succeeded",
                "commit": "b" * 40,
            }

        execution.broker.call = AsyncMock(side_effect=broker_call)
        result = await execute(
            tools,
            json.dumps(
                dict(kind="commit", base_commit="a" * 40, branch="fix", message="Fix")
            ),
            5,
        )
        self.assertEqual(result["state"], "succeeded")
        tools.workspace.invoke.assert_awaited_once()
        tools.emit.assert_awaited_once()
        issued = calls[0]
        digest = hashlib.sha256(
            base64.b64decode(issued["snapshot_content"])
        ).hexdigest()
        self.assertEqual(issued["action"]["change_digest"], digest)
        self.assertEqual(issued["action"]["snapshot"]["sha256"], digest)
        sent = next(v for v in calls if v["operation"] == "execute")
        self.assertEqual(
            set(sent), {"session_id", "generation", "operation", "operation_id"}
        )
        self.assertEqual(sent["operation_id"], "operation")
