"""Work verification is command evidence bound to the latest workspace edit."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from services.orchestrator import code_mode, code_worker
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.code_tools import WorkTools
from services.orchestrator.code_verification import (
    Verification,
    annotate,
    explicit_tests,
)
from services.orchestrator.loop import Call, Turn
from services.orchestrator.work_forge_tools import execute
from test_code_mode import FakeModel, LocalWorkspace


class VerificationTests(unittest.TestCase):
    def test_recognized_and_explicit_commands_not_echoed_words(self) -> None:
        tracker = Verification(
            explicit=explicit_tests(
                "Validate with the test command `./scripts/check.sh --all`."
            )
        )
        for command in (
            "python3 -m unittest -v",
            "cd src && /usr/bin/python3 -m pytest",
            "PYTHONPATH=. uv run pytest",
            "npm run test",
            "make test-unit",
            "./scripts/check.sh --all",
        ):
            with self.subTest(command=command):
                self.assertTrue(tracker.is_test(command))
        for command in (
            "echo tests passed",
            "echo pytest",
            "cat test_file.py",
            "./scripts/unrelated.sh",
        ):
            with self.subTest(command=command):
                self.assertFalse(tracker.is_test(command))

    def test_latest_result_and_edit_order(self) -> None:
        tracker = Verification()
        self.assertEqual(tracker.status().status, "not_run")
        for code, expected in (
            (1, "failed"),
            (0, "passed"),
            (7, "failed"),
            (0, "passed"),
        ):
            tracker.observe(
                "run_command",
                {"command": "pytest"},
                {"exit_code": code, "workspace_changed": False},
            )
            self.assertEqual(tracker.status().status, expected)
        self.assertEqual([r["exit_code"] for r in tracker.runs], [1, 0, 7, 0])
        tracker.observe("edit_file", {}, {"diff": "changed"})
        self.assertEqual(tracker.status().status, "not_run")
        self.assertIn("pytest exited 0", str(tracker.refusal()))
        tracker.observe(
            "run_command",
            {"command": "pytest"},
            {"exit_code": 0, "workspace_changed": False},
        )
        self.assertIsNone(tracker.refusal())
        tracker.observe(
            "run_command",
            {"command": "sed -i s/a/b/ source.py"},
            {"exit_code": 0, "workspace_changed": True},
        )
        self.assertEqual(tracker.status().status, "not_run")

    def test_masked_or_skipped_tests_cannot_establish_success(self) -> None:
        for command in (
            "pytest || true",
            "true || pytest",
            "pytest; echo done",
            "pytest | cat",
            "if false; then pytest; fi",
            "pytest --collect-only",
            "python3 -m unittest --help",
        ):
            with self.subTest(command=command):
                tracker = Verification()
                tracker.observe(
                    "run_command",
                    {"command": command},
                    {"exit_code": 0, "workspace_changed": False},
                )
                self.assertNotEqual(tracker.status().status, "passed")
                self.assertIsNotNone(tracker.refusal())

    def test_timeouts_and_failed_edits(self) -> None:
        tracker = Verification()
        tracker.observe(
            "run_command",
            {"command": "pytest"},
            {"exit_code": 0, "workspace_changed": False},
        )
        tracker.observe("edit_file", {}, {"error": "old_text_missing"})
        self.assertEqual(tracker.status().status, "passed")
        tracker.observe(
            "run_command",
            {"command": "pytest"},
            {"exit_code": 0, "timed_out": True, "workspace_changed": False},
        )
        self.assertEqual(tracker.status().status, "failed")
        tracker.observe(
            "run_command", {"command": "pytest"}, {"error": "code_tool_timeout"}
        )
        self.assertEqual(tracker.status().status, "failed")
        self.assertIsNone(tracker.status().exit_code)

    def test_append_only_correction_and_negation(self) -> None:
        status = Verification().status()
        for text in (
            "The issue is fixed.",
            "All tests pass.",
            "Repaired the parser.",
            "La panne est corrigee.",
        ):
            corrected, changed = annotate(text, status)
            self.assertTrue(changed)
            self.assertTrue(corrected.startswith(text + "\n\nVerification: not_run"))
        for text in (
            "The issue is not fixed.",
            "Tests did not pass.",
            "I could not fix it.",
            "The repair might work.",
        ):
            self.assertEqual(annotate(text, status), (text, False))


class WorkVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        env = patch.dict(
            os.environ,
            {
                "ATLAS_CODE_STATE_DIR": str(self.root / "state"),
                "ATLAS_PROJECT_MODE": "1",
                "ATLAS_STABLE": "0",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.workspace = LocalWorkspace(str(uuid4()))
        self.workspace.root = self.root / "workspace"
        await self.workspace.prepare()
        self.execution = MagicMock()
        self.execution.check = AsyncMock()
        self.execution.session = {
            "effective_limits": {
                "command_memory_bytes": 256 * 1024**2,
                "command_seconds": 30,
            }
        }
        self.tools = WorkTools(self.workspace, self.execution)
        emitter = patch.object(self.tools, "emit", AsyncMock())
        emitter.start()
        self.addCleanup(emitter.stop)

    async def command(self, command: str) -> None:
        await self.tools.execute(
            Call("c", "run_command", json.dumps({"command": command})), 5
        )

    async def test_worker_shell_edit_and_latest_failure(self) -> None:
        (self.workspace.root / "test_example.py").write_text(
            "import unittest\nclass Example(unittest.TestCase):\n def test_value(self): self.assertEqual(1, 1)\n"
        )
        await self.command("python3 -m unittest -v")
        self.assertEqual(self.tools.verification.status().status, "passed")
        await self.command("printf 'value = 2\n' > source.py")
        self.assertEqual(self.tools.verification.status().status, "not_run")
        await self.command("python3 -m unittest -v")
        await self.command("python3 -m unittest missing_module")
        self.assertEqual(self.tools.verification.status().status, "failed")
        self.assertEqual(
            [r["exit_code"] for r in self.tools.verification.runs], [0, 0, 1]
        )

    async def test_turn_structured_status_and_preserved_answer(self) -> None:
        request = ChatRequest.model_validate(
            dict(
                model=code_mode.code_profile(),
                messages=[
                    dict(role="user", content="Repair and test with `./check.sh`.")
                ],
            )
        )
        model = FakeModel([Turn("The issue is fixed. Tests pass.")])
        result = await code_mode.execute_turn(
            request, model, self.tools, code_mode.settings()
        )
        self.assertEqual(result.verification.status, "not_run")
        self.assertTrue(
            result.text.startswith(
                "The issue is fixed. Tests pass.\n\nVerification: not_run"
            )
        )
        self.assertTrue(self.tools.verification.is_test("./check.sh"))

    async def test_publication_refused_before_remote_side_effects(self) -> None:
        args = json.dumps(
            dict(
                kind="publish_change",
                base_commit="a" * 40,
                branch="repair",
                target="main",
                message="Repair",
                title="Repair",
                body="Change",
            )
        )
        for state in ("not_run", "failed", "edited"):
            self.tools.verification = Verification()
            if state != "not_run":
                self.tools.verification.observe(
                    "run_command",
                    {"command": "pytest"},
                    {
                        "exit_code": 1 if state == "failed" else 0,
                        "workspace_changed": state == "edited",
                    },
                )
            result = await execute(self.tools, args, 5)
            self.assertEqual(result["error"], "tests_not_verified")
            self.assertIn(
                "tests have not passed since the last edit:", result["message"]
            )
            self.execution.broker.public.assert_not_called()
            self.execution.broker.call.assert_not_called()

    async def test_worker_detects_test_command_mutating_source(self) -> None:
        result = code_worker.perform(
            self.workspace.root,
            "run_command",
            {"command": "python3 -c \"open('source.py', 'w').write('x=1')\""},
        )
        self.assertTrue(result["workspace_changed"])

    async def test_passing_publication_reaches_existing_approval_boundary(self) -> None:
        self.tools.verification.observe(
            "run_command",
            {"command": "pytest"},
            {"exit_code": 0, "workspace_changed": False},
        )
        self.execution.session["repository_id"] = "selected"
        self.execution.broker.public = AsyncMock(return_value=(200, {"items": []}))
        from services.retrieval.project_identity import ProjectError

        with self.assertRaises(ProjectError) as caught:
            await execute(
                self.tools,
                json.dumps(
                    dict(
                        kind="publish_change",
                        base_commit="a" * 40,
                        branch="repair",
                        target="main",
                        message="Repair",
                        title="Repair",
                        body="Change",
                    )
                ),
                5,
            )
        self.assertEqual(caught.exception.status, 404)
        self.execution.broker.public.assert_awaited_once_with("GET", "repositories")

    async def test_passing_run_then_file_edit_invalidates_final_claim(self) -> None:
        (self.workspace.root / "test_example.py").write_text(
            "import unittest\nclass Example(unittest.TestCase):\n def test_value(self): self.assertEqual(1, 1)\n"
        )
        request = ChatRequest.model_validate(
            dict(
                model=code_mode.code_profile(),
                messages=[dict(role="user", content="Repair and test")],
            )
        )
        model = FakeModel(
            [
                Turn(
                    "", (Call("1", "run_command", '{"command":"python3 -m unittest"}'),)
                ),
                Turn(
                    "",
                    (Call("2", "write_file", '{"path":"source.py","content":"x=2"}'),),
                ),
                Turn("Fixed. All tests passed."),
            ]
        )
        result = await code_mode.execute_turn(
            request, model, self.tools, code_mode.settings()
        )
        self.assertEqual(result.verification.status, "not_run")
        self.assertEqual(result.verification.exit_code, 0)
        self.assertTrue(
            result.text.startswith("Fixed. All tests passed.\n\nVerification: not_run")
        )

    async def test_progress_mapping_preserves_typed_verification(self) -> None:
        tools = WorkTools(self.workspace, self.execution)
        self.execution.session["id"] = "session"
        self.execution.broker.call = AsyncMock(return_value={"events": []})
        status = tools.verification.status().model_dump()
        await tools.emit(
            "recap",
            label="Done",
            status="completed",
            output={"text": "Proposed change", "verification": status},
        )
        event = self.execution.broker.call.await_args.args[1]["progress"][0]
        self.assertEqual(event["output"]["verification"], status)

    async def test_inline_worker_evidence_and_invalidation(self) -> None:
        command = "python3 - <<'PY'\nvalue = 2\nassert value == 2\nPY"
        await self.command(command)
        self.assertEqual(self.tools.verification.status().status, "passed")
        self.assertIsNone(self.tools.verification.refusal())
        await self.command('echo "OK"')
        self.assertEqual(self.tools.verification.status().command, command)
        await self.command("python3 -c 'value = 2; assert value == 3'")
        self.assertEqual(self.tools.verification.status().status, "failed")
        await self.command(command)
        await self.command("printf 'value = 3\n' > source.py")
        self.assertEqual(self.tools.verification.status().status, "not_run")
        self.assertIsNotNone(self.tools.verification.refusal())
        fresh = WorkTools(self.workspace, self.execution)
        self.assertEqual(fresh.verification.status().status, "not_run")

    async def test_work_progress_retains_command_for_audit(self) -> None:
        tools = WorkTools(self.workspace, self.execution)
        self.execution.session["id"] = "session"
        self.execution.broker.call = AsyncMock(return_value={"events": []})
        arguments = json.dumps({"command": "python3 -c 'assert 2 == 2'"})
        await tools.emit(
            "tool", tool="run_command", status="running", arguments=arguments, output={}
        )
        event = self.execution.broker.call.await_args.args[1]["progress"][0]
        result = json.loads(event["output"]["tool_result"])
        self.assertEqual(json.loads(result["arguments"]), json.loads(arguments))
