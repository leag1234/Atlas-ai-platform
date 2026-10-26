"""REQ-RUN-02/05: real tenant database, signed runner HTTP and SSH execution."""

from __future__ import annotations

import base64
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from services.orchestrator.project_forge import canonical
from services.retrieval.project_identity import ProjectError
from services.runner import api
from services.runner.channel import seal
from services.runner.ssh import fingerprint
from test_project_work import ProjectWorkTests

ROOT = Path(__file__).resolve().parents[1]


class RunnerRegistryTests(ProjectWorkTests):
    ssh_container: str
    ssh_port: int
    host_public: str

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        subprocess.run(
            ["docker", "build", "-q", "-t", "atlas-runner-test:local", "tests/runner"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            timeout=180,
        )
        cls.ssh_container = "atlas-runner-registry-" + uuid4().hex[:10]
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "--name",
                cls.ssh_container,
                "-p",
                "127.0.0.1::2222",
                "atlas-runner-test:local",
            ],
            check=True,
            capture_output=True,
        )
        cls.addClassCleanup(
            subprocess.run,
            ["docker", "rm", "-f", cls.ssh_container],
            check=True,
            capture_output=True,
        )
        reply = subprocess.run(
            ["docker", "port", cls.ssh_container, "2222"],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.ssh_port = int(reply.stdout.strip().rsplit(":", 1)[1])
        cls.host_public = subprocess.run(
            [
                "docker",
                "exec",
                cls.ssh_container,
                "cat",
                "/etc/ssh/ssh_host_ed25519_key.pub",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory(dir=ROOT / "BRAIN")
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(
            os.environ,
            {
                "ATLAS_RUNNER_STATE": temporary.name + "/state",
                "ATLAS_RUNNER_KEYS": temporary.name + "/keys",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        api.broker.cache_clear()
        self.addCleanup(api.broker.cache_clear)
        self.runner = TestClient(api.app)
        self.addCleanup(self.runner.close)
        transport = patch("services.retrieval.project_runners.call", self.runner_call)
        transport.start()
        self.addCleanup(transport.stop)
        self.admin_path = f"/api/atlas/orgs/{self.org}/runner-targets"
        # Admin content access remains an explicit project grant.
        granted = self.request(
            "PUT",
            self.project_path() + "/grants/" + self.admin,
            "owner",
            dict(role="editor"),
            match=0,
        )
        self.assertEqual(granted.status_code, 200, granted.text)
        import psycopg
        from psycopg.types.json import Jsonb

        for name in [key for key in self.policy["quotas"] if key.endswith("_bytes")]:
            if name in self.policy["quotas"]:
                self.policy["quotas"][name] = 16 * 1024 * 1024
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.installation SET ceilings=%s",
                (Jsonb(self.policy),),
            )
            db.execute(
                "UPDATE atlas_projects.organizations SET policy=%s WHERE org_id=%s",
                (Jsonb(self.policy), self.org),
            )
        self.assertEqual(self.acquire().status_code, 200)

    def runner_call(
        self, operation: str, auth: dict[str, Any], **data: Any
    ) -> dict[str, Any]:
        response = self.runner.post(
            "/internal/runner/" + operation,
            json=dict(permit=seal(operation, auth), **data),
            headers={
                "X-Atlas-Execution-Secret": os.environ["ATLAS_PROJECT_EXECUTION_SECRET"]
            },
        )
        if response.status_code != 200:
            raise ProjectError(response.status_code, str(response.json()["error"]))
        return dict(response.json())

    def register(self, mode: str = "ask") -> dict[str, Any]:
        result = self.request("POST", self.admin_path + "/credentials", "admin", {})
        self.assertEqual(result.status_code, 200, result.text)
        key = result.json()
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                self.ssh_container,
                "sh",
                "-c",
                "cat >> /home/runner/.ssh/authorized_keys; chown -R runner:runner /home/runner/.ssh; chmod 700 /home/runner/.ssh; chmod 600 /home/runner/.ssh/authorized_keys",
            ],
            input=(key["public_key"] + "\n").encode(),
            check=True,
            capture_output=True,
        )
        value = dict(
            name="Test Linux",
            host="127.0.0.1",
            port=self.ssh_port,
            user="runner",
            work_root="/home/runner/work",
            credential_ref=key["credential_ref"],
            credential_fingerprint=key["fingerprint"],
            host_key_fingerprints=[fingerprint(self.host_public)],
            permission_mode=mode,
            allow_rules=["cat *"],
            deny_rules=["sudo *"],
        )
        created = self.request("POST", self.admin_path, "admin", value)
        self.assertEqual(created.status_code, 201, created.text)
        target = created.json()
        probe = self.request(
            "POST", self.admin_path + "/" + target["id"] + "/probe", "admin", {}
        )
        self.assertEqual(probe.status_code, 200, probe.text)
        self.assertEqual(probe.json()["status"], "available", probe.text)
        grant = self.request(
            "PUT",
            self.project_path() + "/runner-targets/" + target["id"] + "/grant",
            "admin",
            dict(enabled=True),
        )
        self.assertEqual(grant.status_code, 200, grant.text)
        return dict(target)

    def remote(self, operation: str, arguments: dict[str, Any], **extra: Any) -> Any:
        return self.broker(
            "remote",
            dict(self.fence(), operation=operation, arguments=arguments, **extra),
        )

    def prepare(self, target: dict[str, Any], command: str = "cat demo.txt") -> Any:
        raw = canonical(
            dict(
                files=[
                    dict(
                        path="demo.txt",
                        mode="100644",
                        content=base64.b64encode(
                            b"remote acceptance output\n"
                        ).decode(),
                    )
                ]
            )
        )
        return self.remote(
            "prepare",
            dict(target=target["id"], command=command, wait_s=0),
            snapshot_content=base64.b64encode(raw).decode(),
        )

    def test_runner_work_target_list_emits_valid_scoped_progress(self) -> None:
        import asyncio
        from services.retrieval.project_scoped_api import store
        from services.orchestrator.code_mode import WorkExecution
        from services.orchestrator.code_tools import WorkTools
        from services.orchestrator.code_workspace import Workspace
        from services.orchestrator.loop import Call
        from services.orchestrator.work_tool_selection import WorkToolSelection

        target = self.register()
        fixture = self

        class Client:
            org = fixture.org
            project = fixture.project

            async def call(
                self, operation: str, data: dict[str, Any]
            ) -> dict[str, Any]:
                response = fixture.broker(operation, data)
                fixture.assertEqual(response.status_code, 200, response.text)
                return dict(response.json())

        execution = WorkExecution(Client(), self.session, self.turn_id)
        execution.session = self.request(
            "GET",
            self.project_path() + "/work-sessions/" + self.session["id"],
            "editor",
        ).json()
        tools = WorkTools(Workspace(str(uuid4())), execution)
        tools.selection = WorkToolSelection("remote")
        result = asyncio.run(tools.execute(Call("list", "list_targets", "{}"), 10))
        items = result["items"]
        assert isinstance(items, list)
        self.assertEqual(items[0]["id"], target["id"])
        with store(self.app_dsn).scope(
            self.identity("editor", self.project), self.org, self.project
        ) as scope:
            row = scope.db.execute(
                "SELECT data FROM atlas_projects.work_progress WHERE org_id=%s AND project_id=%s AND data->>'tool'='list_targets' ORDER BY seq DESC LIMIT 1",
                (self.org, self.project),
            ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            self.assertIn(target["id"], row["data"]["output"]["tool_result"])

    def test_runner_work_remote_run_records_approval_executes_and_archives(
        self,
    ) -> None:
        import asyncio
        import io
        import tarfile
        from services.orchestrator.code_mode import WorkExecution
        from services.orchestrator.code_tools import WorkTools
        from services.orchestrator.code_workspace import Workspace
        from services.orchestrator.loop import Call
        from services.orchestrator.work_tool_selection import WorkToolSelection
        from services.retrieval.project_work_contract import validate

        target = self.register()
        fixture = self
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            data = b"remote Work adapter acceptance\n"
            entry = tarfile.TarInfo("demo.txt")
            entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))

        class Scratch(Workspace):
            def metadata(self) -> dict[str, Any]:
                return dict(baseline={})

            async def invoke(
                self, operation: str, data: dict[str, Any], timeout: float
            ) -> dict[str, Any]:
                fixture.assertEqual(operation, "export")
                return dict(archive=base64.b64encode(archive.getvalue()).decode())

        class Client:
            org = fixture.org
            project = fixture.project
            approval: dict[str, Any] | None = None

            async def call(
                self, operation: str, data: dict[str, Any]
            ) -> dict[str, Any]:
                if (
                    operation == "remote"
                    and data["operation"] == "status"
                    and self.approval
                ):
                    decision = fixture.request(
                        "POST",
                        fixture.project_path()
                        + "/approvals/"
                        + self.approval["id"]
                        + "/decision",
                        "editor",
                        dict(allow=True, action_digest=self.approval["action_digest"]),
                    )
                    fixture.assertEqual(decision.status_code, 200, decision.text)
                    self.approval = None
                response = fixture.broker(operation, data)
                fixture.assertEqual(response.status_code, 200, response.text)
                value = dict(response.json())
                if operation == "remote" and data["operation"] == "prepare":
                    self.approval = value["approval"]
                    validate("Approval", self.approval)
                return value

        client = Client()
        session = self.request(
            "GET",
            self.project_path() + "/work-sessions/" + self.session["id"],
            "editor",
        ).json()
        tools = WorkTools(
            Scratch(str(uuid4())), WorkExecution(client, session, self.turn_id)
        )
        tools.selection = WorkToolSelection("remote")
        import json

        result = asyncio.run(
            tools.execute(
                Call(
                    "run",
                    "remote_run",
                    json.dumps(
                        dict(target=target["id"], command="cat demo.txt", wait_s=10)
                    ),
                ),
                30,
            )
        )
        self.assertEqual(result["state"], "succeeded", result)
        self.assertIn("remote Work adapter acceptance", str(result["output"]))
        self.assertTrue(result["log_file_id"])
        closed = self.remote("close", {})
        self.assertEqual(closed.status_code, 200, closed.text)

    def test_runner_registry_roles_isolation_and_no_key_disclosure(self) -> None:
        for role in ("editor", "viewer", "owner"):
            response = self.request("POST", self.admin_path + "/credentials", role, {})
            self.assertEqual(response.status_code, 403)
        target = self.register()
        listing = self.remote("list", {})
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["items"][0]["name"], "Test Linux")
        for hidden in ("credential_ref", "host", "user", "private_key", "public_key"):
            self.assertNotIn(hidden, listing.json()["items"][0])
        other = self.request(
            "GET", f"/api/atlas/orgs/{self.other_org}/runner-targets", "admin"
        )
        self.assertEqual(other.status_code, 404)
        full = self.request("GET", self.admin_path, "admin")
        self.assertNotIn("PRIVATE KEY", full.text)
        self.assertNotIn("public_key", full.text)
        self.assertEqual(full.json()["items"][0]["id"], target["id"])
        forged = self.runner.post(
            "/internal/runner/generate",
            json=dict(permit="forged.signature"),
            headers={
                "X-Atlas-Execution-Secret": os.environ["ATLAS_PROJECT_EXECUTION_SECRET"]
            },
        )
        self.assertEqual(forged.status_code, 409)

    def test_runner_approval_egress_rules_and_durable_log(self) -> None:
        target = self.register("rules")
        pending = self.prepare(target)
        self.assertEqual(pending.status_code, 200, pending.text)
        value = pending.json()
        self.assertEqual(value["state"], "awaiting_approval")
        self.assertTrue(value["approval"]["remote"]["code_egress"])
        arguments = dict(command_id=value["command_id"], wait_s=0)
        self.assertEqual(self.remote("execute", arguments).status_code, 403)
        approval = value["approval"]
        decided = self.request(
            "POST",
            self.project_path() + "/approvals/" + approval["id"] + "/decision",
            "editor",
            dict(allow=True, action_digest=approval["action_digest"]),
        )
        self.assertEqual(decided.status_code, 200, decided.text)
        launched = self.remote("execute", arguments)
        self.assertEqual(launched.status_code, 200, launched.text)
        self.assertEqual(self.remote("execute", arguments).status_code, 410)
        for _ in range(30):
            state = self.remote("status", arguments)
            self.assertEqual(state.status_code, 200, state.text)
            if state.json()["state"] not in {"running", "unknown"}:
                break
            time.sleep(0.1)
        self.assertEqual(state.json()["state"], "succeeded", state.text)
        self.assertIn("remote acceptance output", state.json()["output"])
        self.assertTrue(state.json()["log_file_id"])
        next_command = self.prepare(target)
        self.assertEqual(next_command.json()["state"], "ready", next_command.text)
        denied = self.prepare(target, "sudo reboot")
        self.assertEqual(denied.json()["state"], "denied", denied.text)
        fetched = self.remote(
            "fetch", dict(target=target["id"], path_or_glob="demo.txt")
        )
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(
            fetched.json()["files"][0]["size_bytes"], len(b"remote acceptance output\n")
        )
        closed = self.remote("close", {})
        self.assertEqual(closed.status_code, 200, closed.text)

    def test_runner_public_key_once_and_confirmed_prefix_rule(self) -> None:
        key = str(uuid4())
        first = self.request(
            "POST", self.admin_path + "/credentials", "admin", {}, key=key
        )
        self.assertEqual(first.status_code, 200, first.text)
        second = self.request(
            "POST", self.admin_path + "/credentials", "admin", {}, key=key
        )
        self.assertEqual(second.status_code, 410, second.text)
        self.assertNotIn(first.json()["public_key"], second.text)
        target = self.register("ask")
        pending = self.prepare(target).json()
        approval = pending["approval"]
        decided = self.request(
            "POST",
            self.project_path() + "/approvals/" + approval["id"] + "/decision",
            "editor",
            dict(
                allow=True, always_allow=True, action_digest=approval["action_digest"]
            ),
        )
        self.assertEqual(decided.status_code, 200, decided.text)
        args = dict(command_id=pending["command_id"], wait_s=0)
        self.assertEqual(self.remote("execute", args).status_code, 200)
        for _ in range(60):
            state = self.remote("status", args)
            if state.json().get("state") not in {"running", "unknown"}:
                break
            time.sleep(0.1)
        self.assertEqual(state.json()["state"], "succeeded", state.text)
        allowed = self.prepare(target)
        self.assertEqual(allowed.json()["state"], "ready", allowed.text)
        different = self.prepare(target, "cat other.txt")
        self.assertEqual(different.json()["state"], "awaiting_approval", different.text)
        self.assertFalse(different.json()["approval"]["remote"]["code_egress"])
        self.assertEqual(self.remote("close", {}).status_code, 200)

    def test_runner_target_revision_invalidates_pending_approval(self) -> None:
        target = self.register()
        pending = self.prepare(target).json()
        approval = pending["approval"]
        # Re-probing changes the registered revision, invalidating any old binding.
        changed = self.request(
            "POST", self.admin_path + "/" + target["id"] + "/probe", "admin", {}
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        decided = self.request(
            "POST",
            self.project_path() + "/approvals/" + approval["id"] + "/decision",
            "editor",
            dict(allow=True, action_digest=approval["action_digest"]),
        )
        self.assertEqual(decided.status_code, 403, decided.text)
        self.assertEqual(
            self.remote("execute", dict(command_id=pending["command_id"])).status_code,
            403,
        )
        denied = self.request(
            "PUT",
            self.project_path() + "/runner-targets/" + target["id"] + "/grant",
            "editor",
            dict(enabled=False),
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        revoked = self.request(
            "PUT",
            self.project_path() + "/runner-targets/" + target["id"] + "/grant",
            "admin",
            dict(enabled=False),
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(self.remote("list", {}).json()["items"], [])


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    # Parent tests are discovered in their own module; execute only this feature's additions.
    return unittest.TestSuite(
        RunnerRegistryTests(name)
        for name in loader.getTestCaseNames(RunnerRegistryTests)
        if name.startswith("test_runner_")
    )
