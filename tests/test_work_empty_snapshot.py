"""REQ-RUN-04: an empty selection still supports remote workspace export."""

import base64
import tempfile
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import AsyncMock, MagicMock

from services.orchestrator.code_mode import WorkExecution
from services.orchestrator.code_worker import perform
from services.orchestrator.code_workspace import Workspace
from services.orchestrator.work_remote_tools import snapshot


class EmptySnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_selection_can_export_new_remote_work(self) -> None:
        with tempfile.TemporaryDirectory(dir="BRAIN") as directory:
            root = Path(directory).resolve()

            async def invoke(
                action: str, arguments: dict[str, Any], timeout: float
            ) -> dict[str, Any]:
                return perform(root, action, arguments)

            workspace = MagicMock(spec=Workspace)
            workspace.invoke = AsyncMock(side_effect=invoke)
            execution = WorkExecution(
                None,
                dict(base_file_versions=[], effective_limits=dict(expanded_bytes=1024)),
                "request",
            )
            await execution.snapshot(workspace)
            workspace.save.assert_called_once()
            baseline = workspace.save.call_args.args[0]["baseline"]
            self.assertRegex(baseline, r"^[a-f0-9]{40,64}$")
            exported = perform(root, "export", dict(baseline=baseline))
            self.assertEqual(
                snapshot(base64.b64decode(exported["archive"]), None, 1024),
                b'{"files":[]}',
            )
            perform(root, "write_file", dict(path="proof.txt", content="remote work"))
            exported = perform(root, "export", dict(baseline=baseline))
            self.assertIn(
                b'"path":"proof.txt"',
                snapshot(base64.b64decode(exported["archive"]), None, 1024),
            )
