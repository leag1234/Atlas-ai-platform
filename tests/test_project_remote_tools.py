"""Additive remote contracts, bounded summaries and Work tool discovery."""

import base64
import io
import json
from pathlib import Path
import tarfile
import unittest
import uuid
from typing import Any, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from services.orchestrator.work_remote_tools import execute, snapshot
from services.orchestrator.loop import Call
from services.orchestrator.work_tool_selection import WorkToolSelection, select_group
from services.runner.contract import RemoteRun, Target, SCHEMAS
from services.runner.results import bounded, report
from services.runner.policy import RunnerError, prefix_rule
from test_project_contracts import SchemaValidator

ROOT = Path(__file__).resolve().parents[1]


class RemoteToolTests(unittest.TestCase):
    def test_work_tools_return_named_remote_error_and_next_step(self) -> None:
        import asyncio

        from services.orchestrator.code_tools import WorkTools

        async def fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise ValueError("runner_target_unavailable")

        class Execution:
            broker = MagicMock()
            session = dict(id="s", environment={})
            request_id = "r"

            async def check(self) -> None:
                return None

        with patch(
            "services.orchestrator.work_remote_tools.execute",
            side_effect=fail,
        ):
            tools = WorkTools(MagicMock(), Execution())
            emitter = patch.object(tools, "emit", new=AsyncMock())
            emitter.start()
            self.addCleanup(emitter.stop)
            result = asyncio.run(
                tools.execute(
                    Call(
                        "run",
                        "remote_run",
                        json.dumps(dict(target="target", command="true", wait_s=0)),
                    ),
                    1,
                )
            )
        self.assertEqual(result["error"], "runner_target_unavailable")
        next_step = result["next_step"]
        assert isinstance(next_step, str)
        self.assertIn("Correct the command or target", next_step)

    def busy_start(self, failures: Sequence[Exception], timeout: float) -> Any:
        import asyncio

        from services.orchestrator import work_remote_tools

        calls: list[str] = []
        pending = list(failures)

        async def invoke(operation: str, values: dict[str, Any]) -> dict[str, Any]:
            calls.append(operation)
            if pending:
                raise pending.pop(0)
            return dict(state="running", command_id="c")

        tools = MagicMock()
        tools.emit = AsyncMock()
        tools.execution.check = AsyncMock()
        with patch.object(work_remote_tools, "BUSY_POLL_S", 0.01):
            try:
                result: Any = asyncio.run(
                    work_remote_tools.start(
                        tools, invoke, dict(command_id="c"), timeout
                    )
                )
            except Exception as exc:
                result = exc
        return result, calls, tools

    def test_remote_run_waits_for_a_slot_on_a_busy_machine(self) -> None:
        from services.retrieval.project_identity import ProjectError

        busy = [ProjectError(429, "runner_concurrency_limit") for _ in range(2)]
        result, calls, tools = self.busy_start(busy, 60)
        self.assertEqual(result, dict(state="running", command_id="c"))
        self.assertEqual(calls, ["execute"] * 3)
        # The user is told once why nothing moves, not once per attempt.
        self.assertEqual(tools.emit.await_count, 1)
        self.assertIn("busy", tools.emit.await_args.kwargs["label"])

    def test_remote_run_gives_up_on_a_machine_that_stays_busy(self) -> None:
        from services.retrieval.project_identity import ProjectError

        busy = [ProjectError(429, "runner_concurrency_limit") for _ in range(500)]
        result, calls, _ = self.busy_start(busy, 5.05)
        self.assertIsInstance(result, ProjectError)
        self.assertEqual(result.body["error"], "runner_concurrency_limit")
        self.assertGreater(len(calls), 1)

    def test_remote_run_does_not_wait_on_other_refusals(self) -> None:
        from services.retrieval.project_identity import ProjectError

        for refusal in (
            ProjectError(429, "runner_minutes_limit"),
            ProjectError(403, "approval_required"),
        ):
            result, calls, tools = self.busy_start([refusal], 60)
            self.assertIs(result, refusal)
            self.assertEqual(calls, ["execute"])
            tools.emit.assert_not_awaited()

    def test_remote_run_rejects_leading_cd_chain_and_documents_cwd(self) -> None:
        import asyncio

        class Tools:
            execution = None
            workspace = None
            remote_references = None
            remote_in_place = {"laptop": "/home/ada/orbit"}
            remote_targets = [
                dict(id="t-1", name="laptop", folder="/home/ada/orbit"),
                dict(id="t-2", name="target", folder=None),
            ]

            async def emit(self, *args: Any, **kwargs: Any) -> None:
                return None

        def cd_chain(target: str) -> dict[str, Any]:
            return asyncio.run(
                execute(
                    Tools(),
                    "remote_run",
                    json.dumps(
                        dict(
                            target=target,
                            command="cd internal-uuid && python3 -m unittest -v",
                            wait_s=0,
                        )
                    ),
                    1,
                )
            )

        result = cd_chain("target")
        self.assertEqual(result["error"], "runner_cd_chain_not_supported")
        self.assertIn("cwd", result["next_step"])
        self.assertIn("synced project root", result["next_step"])
        # A connected computer starts in the user's folder: no cwd to set.
        connected = cd_chain("Laptop")
        self.assertEqual(connected["error"], "runner_cd_chain_not_supported")
        self.assertIn("already start in /home/ada/orbit", connected["next_step"])
        self.assertIn("leave cwd out", connected["next_step"])
        self.assertNotIn("synced project root", connected["next_step"])

    def test_list_targets_says_a_connected_computer_starts_in_its_folder(
        self,
    ) -> None:
        import asyncio

        items = [
            dict(id="t1", name="portable", folder="/home/ada/orbit"),
            dict(id="t2", name="build", folder=None),
        ]

        class Execution:
            class Broker:
                @staticmethod
                async def call(operation: str, data: dict[str, Any]) -> dict[str, Any]:
                    return dict(items=list(items))

            broker = Broker()

            def fence(self) -> dict[str, Any]:
                return {}

        class Tools:
            execution = Execution()
            remote_in_place: Any = None

        tools = Tools()
        result = asyncio.run(execute(tools, "list_targets", "{}", 1))
        text = result["working_directory"]
        self.assertIn("already start in that folder", text)
        self.assertIn("never the folder's own name", text)
        self.assertIn("On other targets", text)
        self.assertEqual(
            tools.remote_in_place,
            {"t1": "/home/ada/orbit", "portable": "/home/ada/orbit"},
        )
        items.pop()
        only = asyncio.run(execute(Tools(), "list_targets", "{}", 1))
        self.assertNotIn("synced project root", only["working_directory"])

    def test_a_target_is_found_by_the_name_the_user_gives_it(self) -> None:
        import asyncio

        from services.orchestrator import work_remote_tools
        from services.retrieval.project_identity import ProjectError

        listings = [
            [dict(id="t-2", name="build", folder=None)],
            [
                dict(id="t-1", name="laptop", folder="/home/ada/orbit"),
                dict(id="t-2", name="build", folder=None),
            ],
        ]

        async def invoke(operation: str, values: dict[str, Any]) -> dict[str, Any]:
            self.assertEqual(operation, "list")
            return dict(items=listings.pop(0) if len(listings) > 1 else listings[0])

        class Tools:
            remote_targets: Any = None
            remote_in_place: Any = None

        tools = Tools()

        def resolve(name: str) -> str:
            return asyncio.run(work_remote_tools.resolve_target(tools, name, invoke))

        self.assertEqual(resolve("build"), "build")
        # Verified later in the turn: the listing is read again once.
        self.assertEqual(resolve("my-laptop"), "laptop")
        self.assertEqual(resolve("Laptop"), "Laptop")
        self.assertEqual(resolve("ordinateur portable"), "laptop")
        self.assertEqual(resolve("t-2"), "t-2")
        with self.assertRaises(ProjectError):
            resolve("buildbox")

    def follow(
        self,
        statuses: list[dict[str, Any]],
        decided: tuple[int, dict[str, Any]] = (200, {}),
        granted: dict[str, Any] | None = None,
    ) -> tuple[Any, list[str], Any]:
        """work_remote_tools.follow on a connected computer behind the chat."""
        import asyncio

        from services.orchestrator import work_remote_tools

        calls: list[str] = []
        replies = list(statuses)

        async def invoke(operation: str, values: dict[str, Any]) -> dict[str, Any]:
            calls.append(operation)
            if operation == "execute":
                return dict(state="running", command_id="c")
            return replies.pop(0) if replies else dict(state="succeeded")

        tools = MagicMock()
        tools.emit = AsyncMock()
        tools.execution.check = AsyncMock()
        tools.execution.broker.public = AsyncMock(return_value=decided)
        tools.approvals_unattended = True
        tools.lang = "fr"
        tools.machine_approvals = {}
        tools.machine_approval_failures = {}
        tools.machine_approval = granted
        tools.remote_references = None
        args = dict(target="portable", command="git push", cwd=".", wait_s=0)
        try:
            result: Any = asyncio.run(
                work_remote_tools.follow(
                    tools,
                    invoke,
                    args,
                    dict(state="awaiting_approval", command_id="c"),
                    30,
                    True,
                )
            )
        except Exception as exc:
            result = exc
        return result, calls, tools

    @staticmethod
    def waiting(decision: str) -> dict[str, Any]:
        return dict(
            state="awaiting_approval",
            command_id="c",
            approval=dict(id="a", action_digest="d", decision=decision),
        )

    def test_a_closed_approval_is_never_started(self) -> None:
        yes = dict(machine="portable", command="git push", cwd=".", allowed=True)
        for decision in ("invalidated", "expired"):
            with self.subTest(decision=decision):
                result, calls, tools = self.follow(
                    [self.waiting(decision)], granted=yes
                )
                self.assertEqual(result["error"], "approval_closed")
                self.assertIn("asks the user again", result["next_step"])
                self.assertNotIn("execute", calls)
                tools.execution.broker.public.assert_not_awaited()
                self.assertEqual(
                    list(tools.machine_approval_failures.values())[0]["error"],
                    "approval_" + decision,
                )
        refused, calls, tools = self.follow([self.waiting("denied")])
        self.assertEqual(refused["error"], "approval_denied")
        self.assertNotIn("execute", calls)
        self.assertEqual(tools.machine_approval_failures, {})

    def test_the_yes_of_the_conversation_approves_then_starts(self) -> None:
        yes = dict(machine="Portable", command="git push", cwd=".", allowed=True)
        result, calls, tools = self.follow([self.waiting("pending")], granted=yes)
        self.assertEqual(result["state"], "succeeded", result)
        self.assertEqual(calls, ["status", "execute", "status"])
        tools.execution.broker.public.assert_awaited_once()
        self.assertTrue(yes["used"])
        # No yes (or a yes to another command): the question, never a start.
        asked, calls, tools = self.follow([self.waiting("pending")])
        self.assertEqual(asked["state"], "awaiting_approval")
        self.assertIn("git push", asked["approval_question"])
        self.assertNotIn("execute", calls)

    def test_a_refused_decision_is_a_failure_with_the_question_open(self) -> None:
        from services.retrieval.project_identity import ProjectError

        yes = dict(machine="portable", command="git push", cwd=".", allowed=True)
        result, calls, tools = self.follow(
            [self.waiting("pending")], (409, {"error": "session_busy"}), yes
        )
        self.assertEqual(result["error"], "approval_not_recorded")
        self.assertIn("git push", result["approval_question"])
        self.assertNotIn("execute", calls)
        self.assertEqual(
            list(tools.machine_approval_failures.values()),
            [dict(machine="portable", command="git push", error="session_busy")],
        )
        self.assertEqual(len(tools.machine_approvals), 1)

        # Allowed, then refused at start: recorded, raised as before.
        with patch(
            "services.orchestrator.work_remote_tools.start",
            new=AsyncMock(side_effect=ProjectError(403, "approval_required")),
        ):
            failed, calls, tools = self.follow([self.waiting("allowed")])
        self.assertIsInstance(failed, ProjectError)
        self.assertEqual(
            list(tools.machine_approval_failures.values())[0]["error"],
            "approval_required",
        )

    def test_a_written_file_ends_with_a_newline(self) -> None:
        from services.orchestrator.project_forge import snapshot_files
        from services.orchestrator.work_remote_tools import written_snapshot
        import hashlib

        for content, expected in (
            ("print(1)", b"print(1)\n"),
            ("a\nb\n", b"a\nb\n"),
            ("", b""),
        ):
            with self.subTest(content=content):
                _, value = written_snapshot("src/app.py", content)
                files = snapshot_files(value, hashlib.sha256(value).hexdigest())
                self.assertEqual(base64.b64decode(files[0]["content"]), expected)

    def test_remote_run_normalizes_unusable_cwd_to_synced_root(self) -> None:
        folder_id = str(uuid.uuid4())
        cases = {
            "": ".",
            ".": ".",
            folder_id: ".",
            "/" + folder_id: ".",
            "tests/": "tests",
            "./tests": "tests",
        }
        for value, expected in cases.items():
            with self.subTest(cwd=value):
                self.assertEqual(
                    RemoteRun.model_validate(
                        dict(target="target", command="true", cwd=value)
                    ).cwd,
                    expected,
                )
        with self.assertRaises(ValueError):
            RemoteRun.model_validate(
                dict(target="target", command="true", cwd="../outside")
            )

    def test_list_targets_documents_synced_project_root(self) -> None:
        import asyncio

        class Execution:
            class Broker:
                @staticmethod
                async def call(operation: str, data: dict[str, Any]) -> dict[str, Any]:
                    return dict(items=[])

            broker = Broker()

            def fence(self) -> dict[str, Any]:
                return {}

            session: dict[str, Any] = {}

        class Tools:
            execution = Execution()

        result = asyncio.run(execute(Tools(), "list_targets", "{}", 1))
        self.assertIn("synced project root", result["working_directory"])

    def test_contract_examples_validate_and_credentials_are_not_model_arguments(
        self,
    ) -> None:
        contract = yaml.safe_load(
            (ROOT / "contracts/remote-runners.openapi.yaml").read_text()
        )
        validator = SchemaValidator(contract)
        for path in (ROOT / "contracts/examples/remote-runners").glob("*.valid.json"):
            name = path.name.split(".")[0]
            value = json.loads(path.read_text())
            schema = contract["components"]["schemas"][name]
            validator.validate(value, schema)
            model = Target if name == "RunnerTargetInput" else SCHEMAS[name]
            model.model_validate(value)
        for schema in SCHEMAS.values():
            self.assertNotIn("credential", json.dumps(schema.model_json_schema()))
            self.assertNotIn("private_key", json.dumps(schema.model_json_schema()))

    def test_remote_group_is_available_and_stays_bounded(self) -> None:
        from services.orchestrator.work_machine_tools import NAMES as MACHINE_TOOLS

        selection = WorkToolSelection("remote")
        # The user's own machines are set up from the same group (2026-11-11).
        self.assertEqual(
            set(selection.names)
            - {"update_plan", "request_approval", "switch_tool_group"},
            set(SCHEMAS) | (set(MACHINE_TOOLS) - {"machine_connect"}),
        )
        selection.reopen_enabled = True
        selection.memory_enabled = True
        self.assertLessEqual(len(selection.declarations()), 16)
        selection.switch('{"group":"remote","focus":"remote_fetch"}')
        selection.declarations()
        self.assertIn("remote_fetch", selection.names)
        for text in [
            "Run on the remote target",
            "RUN TESTS ON THE BUILD SERVER",
            "Execute sur le serveur dedie",
            "Use the registered machine",
        ]:
            self.assertEqual(select_group(text, []), "remote")
        self.assertEqual(prefix_rule("make test"), "make test *")
        with self.assertRaises(RunnerError):
            prefix_rule("make test && sudo reboot")

    def test_remote_discovery_is_additive_until_targets_are_available(self) -> None:
        selection = WorkToolSelection("code")
        switch = selection.catalog["switch_tool_group"]["function"]
        assert isinstance(switch, dict)
        schema = switch["parameters"]
        self.assertNotIn("remote", schema["properties"]["group"]["enum"])
        with self.assertRaisesRegex(ValueError, "No remote targets"):
            selection.switch('{"group":"remote"}')
        selection.enable_remote()
        selection.switch('{"group":"remote"}')
        self.assertIn(
            "remote_run",
            [
                d["function"]["name"]
                for d in selection.declarations()
                if isinstance(d["function"], dict)
            ],
        )

    def test_snapshot_strips_only_selected_repository_folder_and_preserves_modes(
        self,
    ) -> None:
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:gz") as archive:
            for name, content in [
                ("selected/run.sh", b"echo done"),
                ("other/private.txt", b"do not send"),
            ]:
                entry = tarfile.TarInfo(name)
                entry.mode = 0o755
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
        value = json.loads(snapshot(raw.getvalue(), "selected", 1024))
        self.assertEqual(
            value["files"],
            [
                dict(
                    path="run.sh",
                    mode="100755",
                    content=base64.b64encode(b"echo done").decode(),
                )
            ],
        )
        with self.assertRaisesRegex(ValueError, "runner_snapshot_limit"):
            snapshot(raw.getvalue(), "selected", 1)

    def test_reports_and_summary_limits(self) -> None:
        junit = b'<testsuite><testcase name="pass"/><testcase name="bad"><failure>detail</failure></testcase><testcase name="later"><skipped/></testcase></testsuite>'
        parsed = report(junit, "junit.xml")
        self.assertEqual(
            parsed, dict(tests=3, failed=1, skipped=1, first_failures=["bad"])
        )
        self.assertEqual(
            report(b"1..2\nok 1 works\nnot ok 2 broken\n", "tests.tap")["failed"], 1
        )
        self.assertIn(
            "warning",
            report(b'<!DOCTYPE x [<!ENTITY e "secret">]><x>&e;</x>', "test.xml"),
        )
        value: dict[str, Any] = dict(
            state="failed",
            output="long log\n" * 10000,
            exit_code=1,
            log_file_id="stored",
        )
        summary = bounded(value)
        self.assertLessEqual(len(json.dumps(summary).encode()), 8192)
        self.assertEqual(summary["log_file_id"], "stored")
        self.assertEqual(summary["trust"], "untrusted")

    def test_compose_keys_only_mounted_in_runner_and_worker_stays_networkless(
        self,
    ) -> None:
        services = yaml.safe_load((ROOT / "compose.project.yaml").read_text())[
            "services"
        ]
        holders = [
            name
            for name, service in services.items()
            if any("runner-keys:" in str(mount) for mount in service.get("volumes", []))
        ]
        self.assertEqual(holders, ["runner"])
        self.assertNotIn("ports", services["runner"])
        self.assertNotIn("docker.sock", str(services["runner"]))
        self.assertEqual(
            services["runner"]["environment"]["ATLAS_RUNNER_FIREWALL"], "1"
        )
        # Work workers stay networkless unless the operator opts in to registry egress.
        from unittest import mock
        from services.orchestrator import code_workspace

        with mock.patch.dict("os.environ", {"ATLAS_WORK_EGRESS": ""}):
            workspace = code_workspace.Workspace(str(uuid.uuid4()))
            args = workspace.docker_arguments([], "pass", "atlas-code-test")
        self.assertEqual(args[args.index("--network") + 1], "none")


if __name__ == "__main__":
    unittest.main()
