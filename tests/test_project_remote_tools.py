"""Additive remote contracts, bounded summaries and Work tool discovery."""

import base64
import io
import json
from pathlib import Path
import tarfile
import unittest
import uuid
from typing import Any
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

    def test_remote_run_rejects_leading_cd_chain_and_documents_cwd(self) -> None:
        import asyncio

        class Tools:
            execution = None
            workspace = None
            remote_references = None

            async def emit(self, *args: Any, **kwargs: Any) -> None:
                return None

        result = asyncio.run(
            execute(
                Tools(),
                "remote_run",
                json.dumps(
                    dict(
                        target="target",
                        command="cd internal-uuid && python3 -m unittest -v",
                        wait_s=0,
                    )
                ),
                1,
            )
        )
        self.assertEqual(result["error"], "runner_cd_chain_not_supported")
        self.assertIn("cwd", result["next_step"])

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
        selection = WorkToolSelection("remote")
        self.assertEqual(
            set(selection.names)
            - {"update_plan", "request_approval", "switch_tool_group"},
            set(SCHEMAS),
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
