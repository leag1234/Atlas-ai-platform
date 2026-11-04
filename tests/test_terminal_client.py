"""Terminal responses are untrusted; credentials never enter model history."""

import os
import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator.terminal_client import safe_result, output_path, Publish
from services.orchestrator.file_results import render_files
from services.orchestrator.terminal_client import TerminalClient


class CompleteOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_command_output_and_honest_truncation(self) -> None:
        with patch.dict(os.environ, {"ATLAS_TERMINAL_ENABLED": "1"}):
            client = TerminalClient("output-test")
        for truncated in (False, True):
            with patch.object(
                client,
                "isolated",
                AsyncMock(
                    return_value={
                        "status": "done",
                        "exit_code": 0,
                        "output": [{"type": "output", "data": "full document"}],
                        "truncated": truncated,
                    }
                ),
            ):
                result = await client.execute("cat document.txt", 15)
                if truncated:
                    self.assertEqual(result["error"], "terminal_output_truncated")
                    self.assertNotIn("output", result)
                else:
                    self.assertIn("full document", str(result["output"]))


class TerminalClientTests(unittest.TestCase):
    def test_publication_requires_strategy_and_renders_verified_link(self) -> None:
        with self.assertRaises(ValueError):
            Publish.model_validate({"path": "/home/user/requests/a/report.docx"})
        url = (
            "/api/v1/terminals/atlas-files/files/serve/home/user/requests/a/report.docx"
        )
        answer = render_files(
            "[Report](https://invented.invalid/report.docx)",
            [url],
            ["regenerated"],
            "fr",
        )
        self.assertNotIn("invented.invalid", answer)
        self.assertIn("Document régénéré", answer)
        self.assertIn("mise en forme", answer)
        self.assertIn(url, answer)

    def test_removes_terminal_key_before_model_history(self) -> None:
        with patch.dict(os.environ, {"OPEN_TERMINAL_API_KEY": "private-test-key"}):
            result = safe_result({"output": [{"data": "key=private-test-key"}]})
        self.assertNotIn("private-test-key", str(result))
        self.assertIn("[REDACTED]", str(result))

    def test_publication_requires_request_directory(self) -> None:
        root = "/home/user/requests/abc"
        self.assertEqual(
            output_path(root, root + "/slides.pptx"), root + "/slides.pptx"
        )
        for path in (
            "/etc/hostname",
            root + "/../another/a.pdf",
            root + "/.hidden",
            root + "/script.py",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                output_path(root, path)


class IsolationTests(unittest.TestCase):
    def test_request_container_never_mounts_other_users_workspaces(self) -> None:
        # Code or project mode on a non-stable, possibly multi-user instance: the
        # per-command container must still see only its own request directory.
        for flags in (
            {"ATLAS_CODE_MODE": "1", "ATLAS_PROJECT_MODE": "0"},
            {"ATLAS_CODE_MODE": "0", "ATLAS_PROJECT_MODE": "1"},
            {"ATLAS_CODE_MODE": "1", "ATLAS_PROJECT_MODE": "1"},
        ):
            with (
                self.subTest(flags=flags),
                patch.dict(
                    os.environ,
                    {"ATLAS_TERMINAL_ENABLED": "1", "ATLAS_STABLE": "0", **flags},
                ),
            ):
                from services.orchestrator.code_workspace import enabled, volume

                self.assertTrue(enabled())
                arguments = TerminalClient("abc-123").isolation_arguments("x")
                workspaces = volume()
            mounts = [
                arguments[i + 1] for i, a in enumerate(arguments) if a == "--mount"
            ]
            self.assertEqual(len(mounts), 1)
            self.assertIn("volume-subpath=requests/abc-123", mounts[0])
            joined = " ".join(arguments)
            self.assertNotIn(workspaces, joined)
            self.assertNotIn("/code-workspaces", joined)
            self.assertEqual(arguments[arguments.index("--network") + 1], "none")
            self.assertEqual(arguments[arguments.index("--cap-drop") + 1], "ALL")
            self.assertIn("--read-only", arguments)
            self.assertEqual(arguments[arguments.index("--user") + 1], "1000:1000")

    def test_code_worker_mounts_only_its_own_workspace_subpath(self) -> None:
        import asyncio
        import tempfile
        from uuid import uuid4

        from services.orchestrator import code_workspace

        session, org, project, scope = (str(uuid4()) for _ in range(4))
        with (
            tempfile.TemporaryDirectory() as state,
            patch.dict(
                os.environ,
                {
                    "ATLAS_CODE_STATE_DIR": state,
                    "ATLAS_CODE_MODE": "1",
                    "ATLAS_PROJECT_MODE": "1",
                    "ATLAS_STABLE": "0",
                },
            ),
            patch("services.orchestrator.code_ui.project_mode", return_value=True),
        ):
            for workspace in (
                code_workspace.Workspace(session),
                code_workspace.Workspace(
                    session, org=org, project=project, workspace=scope
                ),
            ):
                with (
                    self.subTest(scoped=workspace.scoped),
                    patch.object(
                        workspace, "_docker", AsyncMock(return_value=b"{}")
                    ) as docker,
                ):
                    asyncio.run(workspace.invoke("list_files", {}, 5))
                mount = docker.call_args.args[0]
                target = mount[mount.index("--mount") + 1]
                self.assertTrue(
                    target.endswith(",dst=/workspace,volume-subpath=" + workspace.key)
                )
                self.assertIn(code_workspace.session_key(session), workspace.key)

    def test_container_sees_only_its_own_request_directory(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ATLAS_TERMINAL_ENABLED": "1",
                "ATLAS_CODE_MODE": "0",
                "ATLAS_PROJECT_MODE": "0",
            },
        ):
            client = TerminalClient("abc-123")
            arguments = client.isolation_arguments("atlas-exec-x")
        mounts = [arguments[i + 1] for i, a in enumerate(arguments) if a == "--mount"]
        self.assertEqual(len(mounts), 1)
        self.assertIn("dst=/home/user/requests/abc-123", mounts[0])
        self.assertIn("volume-subpath=requests/abc-123", mounts[0])
        self.assertEqual(arguments[arguments.index("--network") + 1], "none")
        self.assertEqual(arguments[arguments.index("--cap-drop") + 1], "ALL")
        self.assertIn("--read-only", arguments)
