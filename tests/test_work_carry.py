"""Work carry (option B): a session's workspace survives from one turn to the next."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any
from unittest import mock
from uuid import uuid4

from services.orchestrator import code_mode
from services.orchestrator.code_workspace import (
    Workspace,
    work_carry_days,
    work_carry_enabled,
)


def scoped_workspace(limits: dict[str, object] | None = None) -> Workspace:
    return Workspace(
        str(uuid4()),
        org=str(uuid4()),
        project=str(uuid4()),
        workspace=str(uuid4()),
        generation=1,
        limits=limits or {"expanded_bytes": 1024},
    )


class WorkCarryFlagTests(unittest.TestCase):
    def test_flag_is_off_by_default_and_on_when_set(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATLAS_WORK_CARRY", None)
            self.assertFalse(work_carry_enabled())
        with patch.dict(os.environ, {"ATLAS_WORK_CARRY": "1"}):
            self.assertTrue(work_carry_enabled())

    def test_carry_days_default_and_override(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATLAS_WORK_CARRY_DAYS", None)
            self.assertEqual(work_carry_days(), 7)
        with patch.dict(os.environ, {"ATLAS_WORK_CARRY_DAYS": "3"}):
            self.assertEqual(work_carry_days(), 3)
        with patch.dict(os.environ, {"ATLAS_WORK_CARRY_DAYS": "not-a-number"}):
            self.assertEqual(work_carry_days(), 7)


class CarryScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "ATLAS_PORT_BASE": "5900",
                "ATLAS_CODE_STATE_DIR": str(Path(self.temp.name) / "state"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.workspace = scoped_workspace()

    def test_carry_script_paths_excludes_ownership_and_cap(self) -> None:
        script = self.workspace.carry_script(1024)
        self.assertIn("source='/workspace/" + self.workspace.key + "'", script)
        self.assertIn("carry='" + self.workspace.carry_path() + "'", script)
        self.assertTrue(self.workspace.carry_path().endswith(".carry"))
        for excluded in (".git", "__pycache__", ".pytest_cache", "node_modules"):
            self.assertIn(excluded, script)
        self.assertIn("os.chown(target, 1000, 1000", script)
        self.assertIn("limit=1024", script)
        self.assertIn("size(source) <= limit", script)
        self.assertIn("work_carry_dropped", script)
        self.assertIn("shutil.rmtree(carry)", script)

    def test_carry_script_without_limit_keeps_everything(self) -> None:
        script = self.workspace.carry_script(None)
        self.assertIn("limit=None", script)
        self.assertIn("limit is None or size(source) <= limit", script)

    def test_restore_carry_script_copies_over_generation_as_1000(self) -> None:
        script = self.workspace.restore_carry_script()
        self.assertIn("carry='" + self.workspace.carry_path() + "'", script)
        self.assertIn("target='/workspace/" + self.workspace.key + "'", script)
        self.assertIn("os.chown(destination, 1000, 1000", script)
        self.assertIn("print(count)", script)

    def test_prepare_bootstrap_expires_old_carries(self) -> None:
        captured: list[str] = []

        async def fake_docker(
            mount: list[str], source: str, payload: bytes, timeout: float
        ) -> bytes:
            captured.append(source)
            return b""

        self.workspace._docker = fake_docker  # type: ignore[method-assign]
        import asyncio

        with patch.object(Workspace, "available", return_value=True):
            asyncio.run(self.workspace.prepare())
        bootstrap = captured[0]
        self.assertIn("'/workspace/" + self.workspace.key + "'", bootstrap)
        self.assertIn(".carry", bootstrap)
        self.assertIn("os.path.getmtime(carry) < cutoff", bootstrap)
        self.assertIn("shutil.rmtree(carry)", bootstrap)
        self.assertIn(str(work_carry_days() * 86400), bootstrap)

    def test_cleanup_keep_carry_runs_carry_script_before_removal(self) -> None:
        calls: list[tuple[list[str], str]] = []

        async def fake_docker(
            mount: list[str], source: str, payload: bytes, timeout: float
        ) -> bytes:
            calls.append((mount, source))
            return b""

        self.workspace._docker = fake_docker  # type: ignore[method-assign]
        import asyncio

        asyncio.run(self.workspace.cleanup(keep_carry=True))
        self.assertEqual(len(calls), 2)
        carry_mount, carry_source = calls[0]
        self.assertIn("--user", carry_mount)
        self.assertEqual(carry_mount[carry_mount.index("--user") + 1], "0:0")
        self.assertIn("CHOWN", carry_mount)
        from services.orchestrator.code_workspace import volume

        self.assertIn(f"type=volume,src={volume()},dst=/workspace", carry_mount)
        self.assertIn("carry='" + self.workspace.carry_path() + "'", carry_source)
        removal_source = calls[1][1]
        self.assertIn("'/workspace/" + self.workspace.scope_key + "'", removal_source)
        self.assertNotIn(".carry", removal_source)

    def test_cleanup_without_keep_carry_removes_carry_too(self) -> None:
        sources: list[str] = []

        async def fake_docker(
            mount: list[str], source: str, payload: bytes, timeout: float
        ) -> bytes:
            sources.append(source)
            return b""

        self.workspace._docker = fake_docker  # type: ignore[method-assign]
        import asyncio

        asyncio.run(self.workspace.cleanup())
        self.assertEqual(len(sources), 1)
        self.assertIn("carry='" + self.workspace.carry_path() + "'", sources[0])
        self.assertIn("shutil.rmtree(carry)", sources[0])

    def test_restore_carry_returns_file_count(self) -> None:
        async def fake_docker(
            mount: list[str], source: str, payload: bytes, timeout: float
        ) -> bytes:
            return b"7\n"

        self.workspace._docker = fake_docker  # type: ignore[method-assign]
        import asyncio

        self.assertEqual(asyncio.run(self.workspace.restore_carry()), 7)


class SnapshotCarryTests(unittest.IsolatedAsyncioTestCase):
    def execution(self) -> code_mode.WorkExecution:
        session = dict(
            id=str(uuid4()),
            effective_limits={"expanded_bytes": 100},
            base_file_versions=[],
        )
        broker = MagicMock(org=str(uuid4()), project=str(uuid4()))
        broker.public = AsyncMock(return_value=(200, {}))
        broker.content = AsyncMock(return_value=b"")
        return code_mode.WorkExecution(broker, session, str(uuid4()))

    async def test_snapshot_restores_carry_only_when_enabled(self) -> None:
        execution = self.execution()
        workspace = MagicMock()
        workspace.invoke = AsyncMock(return_value={"baseline": "b"})
        workspace.save = MagicMock()
        workspace.restore_carry = AsyncMock(return_value=3)
        with patch.dict(os.environ, {"ATLAS_WORK_CARRY": "1"}):
            await execution.snapshot(workspace)
        workspace.restore_carry.assert_awaited_once()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATLAS_WORK_CARRY", None)
            workspace.restore_carry.reset_mock()
            await execution.snapshot(workspace)
        workspace.restore_carry.assert_not_awaited()


class FinishCarryTests(unittest.IsolatedAsyncioTestCase):
    async def test_finish_passes_keep_carry_with_the_flag(self) -> None:
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import CodeResult

        session_id, conversation, org, project = (str(uuid4()) for _ in range(4))
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
            base_file_versions=[],
            environment={},
        )
        broker = MagicMock(org=org, project=project)
        broker.public = AsyncMock(return_value=(200, session))
        broker.call = AsyncMock(return_value=dict(session=session, events=[]))
        execution = MagicMock(session=session)
        execution.turn = AsyncMock()
        execution.check = AsyncMock()
        execution.snapshot = AsyncMock()
        execution.broker = broker
        workspace = MagicMock()
        workspace.prepare = AsyncMock()
        workspace.invoke = AsyncMock(return_value={"entries": []})
        workspace.cleanup = AsyncMock()
        execution.workspace.return_value = workspace
        model = MagicMock(spec=code_mode.CodeModel)
        model.spent = 0
        model.input_tokens = model.output_tokens = 0
        model.work_unknown = False
        request = ChatRequest.model_validate(
            dict(
                messages=[dict(role="user", content="Write a memo")],
                conversation_id=conversation,
                project_id=project,
            )
        )
        item = Interaction()

        async def turn(*args: object) -> CodeResult:
            return CodeResult(text="done", state="done", reason="")

        with (
            patch("services.orchestrator.code_workspace.enabled", return_value=True),
            patch(
                "services.orchestrator.code_api.ExecutionBroker", return_value=broker
            ),
            patch(
                "services.orchestrator.code_mode.WorkExecution", return_value=execution
            ),
            patch("services.orchestrator.code_mode.execute_turn", side_effect=turn),
            patch.object(code_mode.CodeModel, "connect", AsyncMock(return_value=model)),
            patch.dict(os.environ, {"ATLAS_WORK_CARRY": "1"}),
        ):
            await code_mode.process_work(
                request,
                item,
                subject="editor",
                org_id=org,
                project_id=project,
                session_id=session_id,
            )
        workspace.cleanup.assert_awaited_once_with(keep_carry=True)

        workspace.cleanup.reset_mock()
        with (
            patch("services.orchestrator.code_workspace.enabled", return_value=True),
            patch(
                "services.orchestrator.code_api.ExecutionBroker", return_value=broker
            ),
            patch(
                "services.orchestrator.code_mode.WorkExecution", return_value=execution
            ),
            patch("services.orchestrator.code_mode.execute_turn", side_effect=turn),
            patch.object(code_mode.CodeModel, "connect", AsyncMock(return_value=model)),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("ATLAS_WORK_CARRY", None)
            await code_mode.process_work(
                request,
                item,
                subject="editor",
                org_id=org,
                project_id=project,
                session_id=session_id,
            )
        workspace.cleanup.assert_awaited_once_with(keep_carry=False)


if __name__ == "__main__":
    unittest.main()


class PrepareBootstrapExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_bootstrap_expires_nested_carries_and_creates_home(
        self,
    ) -> None:
        import tempfile
        import time

        from services.orchestrator import code_workspace as cw

        captured: list[str] = []

        async def fake_docker(
            self: Any, args: Any, source: str, stdin: bytes, timeout: float
        ) -> bytes:
            captured.append(source)
            return b"0"

        async def no_egress(self: Any) -> None:
            return None

        with (
            tempfile.TemporaryDirectory() as state,
            mock.patch.dict(
                os.environ,
                {
                    "ATLAS_PROJECT_MODE": "1",
                    "ATLAS_WORK_EGRESS": "registries",
                    "ATLAS_CODE_STATE_DIR": state,
                },
            ),
            mock.patch.object(cw.Workspace, "_docker", fake_docker),
            mock.patch.object(cw.Workspace, "_start_egress", no_egress),
            mock.patch.object(cw.Workspace, "available", lambda self: True),
        ):
            workspace = cw.Workspace(
                "11111111-1111-1111-1111-111111111111",
                org="22222222-2222-2222-2222-222222222222",
                project="33333333-3333-3333-3333-333333333333",
                workspace="44444444-4444-4444-4444-444444444444",
                generation=2,
            )
            await workspace.prepare()
        with tempfile.TemporaryDirectory() as root:
            script = (
                captured[0]
                .replace("/workspace", root)
                .replace("os.chown(", "(lambda *a, **k: None)(")
            )
            old = os.path.join(root, "a/b/c/old.carry")
            new = os.path.join(root, "a/b/d/new.carry")
            os.makedirs(old)
            os.makedirs(new)
            past = time.time() - 30 * 86400
            os.utime(old, (past, past))
            exec(compile(script, "bootstrap", "exec"), {})
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(new))
            self.assertTrue(os.path.isdir(os.path.join(root, workspace.key + ".home")))
            self.assertTrue(os.path.isdir(os.path.join(root, workspace.key)))
