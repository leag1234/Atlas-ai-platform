"""Personal machines: a member's own SSH targets, without administrator or grant.

Real tenant database with row-level security, the real runner broker over its
signed HTTP channel and a disposable SSH account. Hosts are loopback or
documentation addresses; keys are generated in temporary directories.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
from typing import Any
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from services.retrieval.project_identity import ProjectError
from services.retrieval.project_scoped_api import store
from services.runner.broker import Broker
from services.runner.machines import Machine, install_command
from services.runner.network import vetted
from services.runner.policy import RunnerError
from services.runner.ssh import Keys, fingerprint
from test_runner_registry import RunnerRegistryTests

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFictionalExampleKeyMaterial0000000000000000"
HOSTILE_HOSTS = [
    "example.org; id",
    "example.org;id",
    "$(id)",
    "`id`",
    "exa mple.org",
    "example.org'",
    '"example.org"',
    "-oProxyCommand=id",
    "example.org\n",
    "example.org|id",
    "example.org&&id",
    "[::1]",
    "::1",
    "a..b",
    ".example.org",
    "example.org.",
    "user@example.org",
    "example.org:22",
    "example.org/x",
    "",
    "a" * 254,
]
HOSTILE_USERS = [
    "-l",
    "dev ops",
    "dev;id",
    "$(id)",
    "dev'",
    'dev"',
    "dev\n",
    "dev@example.org",
    "1dev",
    "",
    "d" * 65,
]


class MachineCommandTests(unittest.TestCase):
    def test_machine_install_command_is_exact_and_safely_quoted(self) -> None:
        self.assertEqual(
            install_command("dev", "build.example.org", 22, KEY + " atlas-runner"),
            "ssh dev@build.example.org 'mkdir -p ~/.ssh && echo \""
            + KEY
            + " atlas\" >> ~/.ssh/authorized_keys'",
        )
        command = install_command("dev.ops", "192.0.2.10", 2222, KEY)
        self.assertEqual(
            shlex.split(command),
            [
                "ssh",
                "-p",
                "2222",
                "dev.ops@192.0.2.10",
                'mkdir -p ~/.ssh && echo "' + KEY + ' atlas" >> ~/.ssh/authorized_keys',
            ],
        )
        # The remote part does what it says in a real shell, and nothing else.
        with tempfile.TemporaryDirectory() as home:
            subprocess.run(
                ["sh", "-c", shlex.split(command)[-1]],
                env={"HOME": home, "PATH": os.defpath},
                cwd=home,
                check=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(
                sorted(p.name for p in Path(home).rglob("*")),
                [".ssh", "authorized_keys"],
            )
            self.assertEqual(
                (Path(home) / ".ssh/authorized_keys").read_text(), KEY + " atlas\n"
            )

    def test_machine_install_command_refuses_unvalidated_parts(self) -> None:
        for host in HOSTILE_HOSTS:
            with self.subTest(host=host), self.assertRaises(ValueError):
                install_command("dev", host, 22, KEY)
        for user in HOSTILE_USERS:
            with self.subTest(user=user), self.assertRaises(ValueError):
                install_command(user, "example.org", 22, KEY)
        for port in (0, 65536, -1, True, "22", 22.0, "22; id"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                install_command("dev", "example.org", port, KEY)  # type: ignore[arg-type]
        for key in (
            'ssh-ed25519 AAAA" ; touch owned ; "',
            "ssh-ed25519 $(id)",
            "ssh-ed25519 " + "A" * 39,
            "ssh-rsa " + "A" * 80,
            "ssh-ed25519",
            KEY.replace("ssh-ed25519 ", "ssh-ed25519 '"),
            "",
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                install_command("dev", "example.org", 22, key)

    def test_machine_model_refuses_hostile_input(self) -> None:
        valid = dict(name="Build box", host="build.example.org", user="dev")
        self.assertEqual(Machine.model_validate(valid).permission_mode, "rules")
        self.assertEqual(Machine.model_validate(valid).port, 22)
        cases: list[dict[str, Any]] = [dict(valid, host=h) for h in HOSTILE_HOSTS]
        cases += [dict(valid, user=u) for u in HOSTILE_USERS]
        cases += [dict(valid, port=p) for p in (0, 65536, True, "22", 22.5)]
        cases += [dict(valid, name=n) for n in ("", " padded", "two\nlines", "n" * 121)]
        cases += [
            dict(valid, work_root=w)
            for w in ("relative", "/", "/a/../b", "/a b", "/a;b", "/a//b", "/$HOME")
        ]
        cases += [
            dict(valid, permission_mode="unrestricted"),
            dict(valid, private_key="x"),
            dict(valid, credential_ref=str(uuid4())),
            dict(valid, owner_member_id=str(uuid4())),
            dict(valid, allow_rules=["*"]),
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                Machine.model_validate(case)

    def test_machine_address_must_be_public_or_listed_by_the_operator(self) -> None:
        with patch.dict(os.environ, {"ATLAS_RUNNER_PERSONAL_NETWORKS": ""}):
            for host in (
                "127.0.0.1",
                "10.0.0.5",
                "172.16.0.9",
                "192.168.1.20",
                "169.254.169.254",
                "100.64.0.1",
                "0.0.0.0",
                "192.0.2.10",
                "2130706433",
            ):
                with self.subTest(host=host), self.assertRaises(RunnerError) as denied:
                    vetted(host, 22)
                self.assertEqual(str(denied.exception), "runner_target_address_denied")
        with patch.dict(
            os.environ, {"ATLAS_RUNNER_PERSONAL_NETWORKS": "192.0.2.0/24, 10.0.0.0/8"}
        ):
            self.assertEqual(vetted("192.0.2.10", 22), "192.0.2.10")
            self.assertEqual(vetted("10.0.0.5", 22), "10.0.0.5")
            with self.assertRaises(RunnerError):
                vetted("127.0.0.1", 22)
            # A name answering with one listed and one internal address is refused.
            answers = [
                (2, 1, 6, "", ("192.0.2.10", 22)),
                (2, 1, 6, "", ("172.16.0.9", 22)),
            ]
            with (
                patch("socket.getaddrinfo", return_value=answers),
                self.assertRaises(RunnerError) as denied,
            ):
                vetted("build.example.org", 22)
            self.assertEqual(str(denied.exception), "runner_target_address_denied")
        with (
            patch.dict(os.environ, {"ATLAS_RUNNER_PERSONAL_NETWORKS": "not-a-network"}),
            self.assertRaises(RunnerError) as broken,
        ):
            vetted("192.0.2.10", 22)
        self.assertEqual(str(broken.exception), "runner_egress_policy_failed")

    def test_machine_broker_runs_only_its_owner_commands(self) -> None:
        owner, other, target = str(uuid4()), str(uuid4()), str(uuid4())
        project, session = str(uuid4()), str(uuid4())
        with tempfile.TemporaryDirectory() as root:
            broker = Broker(Path(root) / "state", Keys(Path(root) / "keys"))

            def auth(member: str) -> dict[str, Any]:
                return dict(
                    org_id=str(uuid4()),
                    project_id=project,
                    target=dict(id=target, owner_member_id=owner),
                    grant=dict(target_id=target, project_id=project, enabled=True),
                    session=dict(id=session, target_id=target),
                    command=dict(
                        command="make test",
                        cwd=".",
                        timeout_s=60,
                        decision="auto_mode",
                        member_id=member,
                        runner_session_id=session,
                    ),
                )

            broker.authorize(auth(owner))
            with self.assertRaises(RunnerError) as refused:
                broker.authorize(auth(other))
            self.assertEqual(str(refused.exception), "runner_unauthorized")


class MachineTests(RunnerRegistryTests):
    def setUp(self) -> None:
        super().setUp()
        # The disposable account listens on loopback, which only an operator
        # can open to personal machines.
        networks = patch.dict(
            os.environ, {"ATLAS_RUNNER_PERSONAL_NETWORKS": "127.0.0.0/8"}
        )
        networks.start()
        self.addCleanup(networks.stop)
        self.machines = f"/api/atlas/orgs/{self.org}/machines"
        self.schema = self.contract["components"]["schemas"]["PersonalMachine"]

    def declare(self, role: str = "editor", **changes: Any) -> Any:
        data = dict(
            name="Build box", host="127.0.0.1", port=self.ssh_port, user="runner"
        )
        data.update(changes)
        return self.request("POST", self.machines, role, data)

    def install(self, machine: dict[str, Any]) -> None:
        """What the member does in his own terminal, run inside the account."""
        argv = shlex.split(machine["install_command"])
        self.assertEqual(
            argv[:-1], ["ssh", "-p", str(self.ssh_port), "runner@127.0.0.1"]
        )
        subprocess.run(
            [
                "docker",
                "exec",
                "-u",
                "runner",
                self.ssh_container,
                "sh",
                "-c",
                argv[-1],
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )

    def verify(self, machine: dict[str, Any], role: str = "editor", **data: Any) -> Any:
        return self.request(
            "POST", self.machines + "/" + machine["id"] + "/verify", role, data
        )

    def ready(self, role: str = "editor", **changes: Any) -> dict[str, Any]:
        declared = self.declare(role, **changes)
        self.assertEqual(declared.status_code, 201, declared.text)
        self.install(declared.json())
        verified = self.verify(declared.json(), role)
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertEqual(verified.json()["status"], "available", verified.text)
        return dict(verified.json())

    def run_approved(
        self, machine: dict[str, Any], command: str = "cat demo.txt"
    ) -> Any:
        value = self.prepare(machine, command).json()
        if value["state"] == "awaiting_approval":
            approval = value["approval"]
            decided = self.request(
                "POST",
                self.project_path() + "/approvals/" + approval["id"] + "/decision",
                "editor",
                dict(allow=True, action_digest=approval["action_digest"]),
            )
            self.assertEqual(decided.status_code, 200, decided.text)
        arguments = dict(command_id=value["command_id"], wait_s=0)
        launched = self.remote("execute", arguments)
        self.assertEqual(launched.status_code, 200, launched.text)
        for _ in range(100):
            state = self.remote("status", arguments)
            self.assertEqual(state.status_code, 200, state.text)
            if state.json()["state"] not in {"running", "unknown"}:
                break
            time.sleep(0.1)
        return state.json()

    def rows(self, role: str, statement: str, *values: Any) -> int:
        """Row count seen or changed by a member through row-level security."""
        with store(self.app_dsn).scope(self.identity(role), self.org) as scope:
            cursor = scope.db.execute(statement, values)
            if cursor.description is None:
                return int(cursor.rowcount)
            return len(cursor.fetchall())

    def key_files(self) -> list[Path]:
        directory = Path(os.environ["ATLAS_RUNNER_KEYS"]) / self.org
        return sorted(directory.iterdir()) if directory.exists() else []

    def other_session(self, role: str) -> dict[str, Any]:
        """Hand the single Work lease of the fixture to another member."""
        released = self.turn(
            "release", terminal_state="completed", cleanup_confirmed=True
        )
        self.assertEqual(released.status_code, 200, released.text)
        conversation = str(uuid4())
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "INSERT INTO atlas_projects.legacy_rows VALUES(%s,%s,'conversations',%s,1,%s)",
                (self.org, self.project, conversation, Jsonb(dict(id=conversation))),
            )
        created = self.request(
            "POST",
            self.project_path() + "/work-sessions",
            role,
            dict(conversation_id=conversation, folder_ids=[], base_versions=[]),
        )
        self.assertEqual(created.status_code, 201, created.text)
        fence = dict(
            session_id=created.json()["id"], request_id=str(uuid4()), generation=1
        )
        acquired = self.broker(
            "acquire",
            dict(
                session_id=fence["session_id"],
                request_id=fence["request_id"],
                expected_revision=created.json()["revision"],
                storage_bytes=0,
            ),
            role,
        )
        self.assertEqual(acquired.status_code, 200, acquired.text)
        return fence

    def test_machine_owner_declares_installs_verifies_and_runs_without_grant(
        self,
    ) -> None:
        declared = self.declare()
        self.assertEqual(declared.status_code, 201, declared.text)
        machine = declared.json()
        self.validator.validate(machine, self.schema)
        self.assertEqual(machine["status"], "unverified")
        self.assertEqual(machine["permission_mode"], "rules")
        self.assertEqual(machine["owner_member_id"], self.members["editor"])
        self.assertEqual(machine["host_key_fingerprints"], [])
        self.assertEqual(
            machine["install_command"],
            f"ssh -p {self.ssh_port} runner@127.0.0.1 'mkdir -p ~/.ssh && echo \""
            + machine["public_key"]
            + "\" >> ~/.ssh/authorized_keys'",
        )
        self.assertTrue(machine["public_key"].endswith(" atlas"))
        for hidden in ("credential_ref", "private_key", "PRIVATE KEY"):
            self.assertNotIn(hidden, declared.text)
        self.assertEqual(len(self.key_files()), 1)
        self.assertEqual(self.key_files()[0].stat().st_mode & 0o077, 0)
        # Declared but not verified: no Work session can use it.
        self.assertEqual(self.remote("list", {}).json()["items"], [])
        self.assertEqual(self.prepare(machine).status_code, 404)
        # First verification pins the host key even before the key is installed.
        early = self.verify(machine)
        self.assertEqual(early.status_code, 200, early.text)
        self.assertEqual(early.json()["status"], "unverified")
        self.assertEqual(early.json()["last_probe"]["error"], "credential_unavailable")
        self.assertEqual(
            early.json()["host_key_fingerprints"], [fingerprint(self.host_public)]
        )
        self.assertIsNone(early.json()["verified_at"])
        self.assertEqual(self.remote("list", {}).json()["items"], [])
        self.install(machine)
        verified = self.verify(machine)
        self.assertEqual(verified.status_code, 200, verified.text)
        machine = verified.json()
        self.validator.validate(machine, self.schema)
        self.assertEqual(machine["status"], "available", verified.text)
        self.assertEqual(machine["work_root"], "/home/runner/atlas-work")
        mode = subprocess.run(
            [
                "docker",
                "exec",
                self.ssh_container,
                "stat",
                "-c",
                "%a",
                machine["work_root"],
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(mode, "700")
        self.assertEqual(
            machine["host_key_fingerprints"], [fingerprint(self.host_public)]
        )
        self.assertTrue(machine["verified_at"])
        # No administrator, no grant: the owner's Work session sees and uses it.
        with psycopg.connect(self.admin_dsn) as db:
            row = db.execute(
                "SELECT count(*) FROM atlas_projects.runner_grants"
            ).fetchone()
            assert row is not None
            self.assertEqual(row[0], 0)
        listing = self.remote("list", {})
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(
            [(item["id"], item.get("personal")) for item in listing.json()["items"]],
            [(machine["id"], True)],
        )
        for hidden in (
            "host",
            "user",
            "port",
            "public_key",
            "credential_ref",
            "work_root",
        ):
            self.assertNotIn(hidden, listing.json()["items"][0])
        pending = self.prepare(machine)
        self.assertEqual(pending.status_code, 200, pending.text)
        # The first-use code egress approval of design 18 section 6 is kept.
        self.assertEqual(pending.json()["state"], "awaiting_approval")
        self.assertTrue(pending.json()["approval"]["remote"]["code_egress"])
        self.assertEqual(
            self.remote(
                "execute", dict(command_id=pending.json()["command_id"], wait_s=0)
            ).status_code,
            403,
        )
        finished = self.run_approved(machine)
        self.assertEqual(finished["state"], "succeeded", finished)
        self.assertIn("remote acceptance output", finished["output"])
        again = self.prepare(machine, "cat demo.txt").json()
        self.assertEqual(again["state"], "awaiting_approval", again)
        self.assertFalse(again["approval"]["remote"]["code_egress"])
        by_name = self.remote(
            "fetch", dict(target="Build box", path_or_glob="demo.txt")
        )
        self.assertEqual(by_name.status_code, 200, by_name.text)
        # Administration still lists organization targets only.
        self.assertEqual(
            self.request("GET", self.admin_path, "admin").json()["items"], []
        )
        self.assertEqual(self.remote("close", {}).status_code, 200)
        replayed = self.request("GET", self.machines + "/" + machine["id"], "editor")
        self.assertEqual(replayed.json()["install_command"], machine["install_command"])

    def api_hidden(self, machine: dict[str, Any]) -> None:
        """Every member route answers another member as if nothing existed."""
        path = self.machines + "/" + machine["id"]
        for role in ("owner", "viewer", "ungranted", "admin"):
            with self.subTest(role=role):
                self.assertEqual(
                    self.request("GET", self.machines, role).json()["items"], []
                )
                self.error(self.request("GET", path, role), 404, "not_found")
                self.error(
                    self.request(
                        "PATCH",
                        path,
                        role,
                        dict(name="Taken"),
                        match=machine["revision"],
                    ),
                    404,
                    "not_found",
                )
                self.error(self.verify(machine, role), 404, "not_found")
                self.error(
                    self.verify(machine, role, reset_host_key=True), 404, "not_found"
                )
                self.error(
                    self.request("DELETE", path, role, match=machine["revision"]),
                    404,
                    "not_found",
                )
        # An administrator cannot adopt it as an organization target either.
        self.assertEqual(
            self.request("GET", self.admin_path, "admin").json()["items"], []
        )
        self.assertEqual(
            self.request(
                "POST", self.admin_path + "/" + machine["id"] + "/probe", "admin", {}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.request(
                "PUT",
                self.project_path() + "/runner-targets/" + machine["id"] + "/grant",
                "admin",
                dict(enabled=True),
            ).status_code,
            404,
        )

    def work_hidden(self, machine: dict[str, Any]) -> None:
        """Another member's Work session in the same project cannot list or use it."""
        fence = self.other_session("owner")
        listed = self.broker(
            "remote", dict(fence, operation="list", arguments={}), "owner"
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["items"], [])
        for target in (machine["id"], machine["name"]):
            with self.subTest(target=target):
                used = self.broker(
                    "remote",
                    dict(
                        fence,
                        operation="prepare",
                        arguments=dict(target=target, command="cat demo.txt", wait_s=0),
                        snapshot_content=self.snapshot(),
                    ),
                    "owner",
                )
                self.error(used, 404, "not_found")
                fetched = self.broker(
                    "remote",
                    dict(
                        fence,
                        operation="fetch",
                        arguments=dict(target=target, path_or_glob="demo.txt"),
                    ),
                    "owner",
                )
                self.error(fetched, 404, "not_found")
        still = self.request("GET", self.machines + "/" + machine["id"], "editor")
        self.assertEqual(still.status_code, 200, still.text)
        self.assertEqual(still.json()["revision"], machine["revision"])
        with psycopg.connect(self.admin_dsn) as db:
            row = db.execute(
                "SELECT count(*) FROM atlas_projects.runner_grants"
            ).fetchone()
        self.assertEqual(row, (0,))

    def test_machine_does_not_exist_for_any_other_member(self) -> None:
        machine = self.ready()
        mine = self.request("GET", self.machines, "editor")
        self.assertEqual([m["id"] for m in mine.json()["items"]], [machine["id"]])
        self.api_hidden(machine)
        self.work_hidden(machine)

    def test_machine_row_level_security_hides_it_whatever_the_application_asks(
        self,
    ) -> None:
        machine = self.ready()
        select = "SELECT id FROM atlas_projects.runner_targets WHERE id=%s"
        self.assertEqual(self.rows("editor", select, machine["id"]), 1)
        for role in ("owner", "viewer", "ungranted", "admin"):
            with self.subTest(role=role):
                self.assertEqual(self.rows(role, select, machine["id"]), 0)
                self.assertEqual(
                    self.rows(
                        role,
                        "UPDATE atlas_projects.runner_targets SET data=data WHERE id=%s",
                        machine["id"],
                    ),
                    0,
                )
                self.assertEqual(
                    self.rows(
                        role,
                        "DELETE FROM atlas_projects.runner_targets WHERE id=%s",
                        machine["id"],
                    ),
                    0,
                )
                # Nor can a member mark a machine as owned by somebody else.
                with self.assertRaises(psycopg.Error):
                    self.rows(
                        role,
                        "INSERT INTO atlas_projects.runner_targets(org_id,id,data,owner_member_id) VALUES(%s,%s,'{}',%s)",
                        self.org,
                        str(uuid4()),
                        self.members["editor"],
                    )
        self.assertEqual(self.rows("editor", select, machine["id"]), 1)

    def test_machine_application_refuses_others_without_row_level_security(
        self,
    ) -> None:
        machine = self.ready()
        # The database layer is removed on purpose: any active member may read
        # and write every target row. The application alone must still refuse.
        with psycopg.connect(self.admin_dsn) as db:
            for policy in ("scoped_read", "admin_write", "owner_write"):
                db.execute(f"DROP POLICY {policy} ON atlas_projects.runner_targets")
            db.execute(
                "CREATE POLICY open_all ON atlas_projects.runner_targets FOR ALL"
                " USING(org_id=atlas_projects.org() AND atlas_projects.active())"
                " WITH CHECK(org_id=atlas_projects.org() AND atlas_projects.active())"
            )
        select = "SELECT id FROM atlas_projects.runner_targets WHERE id=%s"
        self.assertEqual(self.rows("owner", select, machine["id"]), 1)
        self.api_hidden(machine)
        self.work_hidden(machine)

    def test_machine_broker_refuses_a_control_permit_from_a_non_owner(self) -> None:
        machine = self.ready()
        with psycopg.connect(self.admin_dsn) as db:
            row = db.execute(
                "SELECT data FROM atlas_projects.runner_targets WHERE id=%s",
                (machine["id"],),
            ).fetchone()
        assert row is not None
        for operation in ("probe", "scan", "forget"):
            with self.subTest(operation=operation), self.assertRaises(ProjectError):
                self.runner_call(
                    operation,
                    dict(
                        org_id=self.org,
                        personal=True,
                        member_id=self.members["owner"],
                        target=row[0],
                    ),
                )
        self.assertEqual(len(self.key_files()), 1)
        own = self.runner_call(
            "probe",
            dict(
                org_id=self.org,
                personal=True,
                member_id=self.members["editor"],
                target=row[0],
            ),
        )
        self.assertEqual(own["status"], "available", own)

    def snapshot(self) -> str:
        import base64
        from services.orchestrator.project_forge import canonical

        return base64.b64encode(
            canonical(
                dict(
                    files=[
                        dict(
                            path="demo.txt",
                            mode="100644",
                            content=base64.b64encode(b"other member\n").decode(),
                        )
                    ]
                )
            )
        ).decode()

    def test_machine_organization_target_still_needs_its_grant(self) -> None:
        organization = self.register("rules")
        machine = self.ready()

        def names() -> list[str]:
            items = self.remote("list", {}).json()["items"]
            return sorted(str(item["name"]) for item in items)

        self.assertEqual(names(), ["Build box", "Test Linux"])
        revoked = self.request(
            "PUT",
            self.project_path() + "/runner-targets/" + organization["id"] + "/grant",
            "admin",
            dict(enabled=False),
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(names(), ["Build box"])
        self.assertEqual(self.prepare(organization).status_code, 404)
        self.assertEqual(self.prepare(machine).status_code, 200)
        # Members still cannot register organization targets.
        self.assertEqual(
            self.request(
                "POST", self.admin_path + "/credentials", "editor", {}
            ).status_code,
            403,
        )
        self.assertEqual(self.remote("close", {}).status_code, 200)

    def test_machine_api_refuses_hostile_input_before_any_key_exists(self) -> None:
        valid = dict(name="Build box", host="build.example.org", user="dev")
        cases: list[dict[str, Any]] = [
            dict(valid, host="example.org; id"),
            dict(valid, host="$(id)"),
            dict(valid, host="exa mple.org"),
            dict(valid, host="example.org'"),
            dict(valid, host='"example.org"'),
            dict(valid, host="-oProxyCommand=id"),
            dict(valid, user="-l"),
            dict(valid, user="dev;id"),
            dict(valid, user="dev ops"),
            dict(valid, port=0),
            dict(valid, port=65536),
            dict(valid, port="22"),
            dict(valid, port=True),
            dict(valid, name=""),
            dict(valid, permission_mode="unrestricted"),
            dict(valid, private_key="x"),
            dict(valid, owner_member_id=self.members["owner"]),
        ]
        for case in cases:
            with self.subTest(case=case):
                refused = self.request("POST", self.machines, "editor", case)
                self.error(refused, 422, "invalid_request")
                self.assertNotIn("id)", refused.text)
        self.assertEqual(
            self.request("GET", self.machines, "editor").json()["items"], []
        )
        self.assertEqual(self.key_files(), [])
        self.assertEqual(
            self.request("POST", self.machines + "?x=1", "editor", valid).status_code,
            422,
        )
        self.assertEqual(
            self.request("GET", self.machines + "/not-a-machine", "editor").status_code,
            404,
        )

    def test_machine_update_limit_replay_and_delete_remove_the_credential(self) -> None:
        key = str(uuid4())
        first = self.request(
            "POST",
            self.machines,
            "editor",
            dict(name="Build box", host="127.0.0.1", port=self.ssh_port, user="runner"),
            key=key,
        )
        again = self.request(
            "POST",
            self.machines,
            "editor",
            dict(name="Build box", host="127.0.0.1", port=self.ssh_port, user="runner"),
            key=key,
        )
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(again.json(), first.json())
        self.assertEqual(len(self.key_files()), 1)
        self.error(self.declare(), 409, "runner_name_conflict")
        with patch("services.retrieval.project_machines.LIMIT", 1):
            self.error(self.declare(name="Second box"), 409, "runner_machine_limit")
        self.assertEqual(len(self.key_files()), 1)
        machine = first.json()
        self.install(machine)
        machine = self.verify(machine).json()
        self.assertEqual(machine["status"], "available", machine)
        path = self.machines + "/" + machine["id"]
        self.error(
            self.request("PATCH", path, "editor", dict(name="Renamed")),
            428,
            "precondition_required",
        )
        self.error(
            self.request("PATCH", path, "editor", dict(name="Renamed"), match=0),
            409,
            "revision_conflict",
        )
        for refused in (
            {},
            dict(host="192.0.2.7"),
            dict(user="root"),
            dict(owner_member_id=self.members["owner"]),
            dict(permission_mode="unrestricted"),
        ):
            with self.subTest(refused=refused):
                self.error(
                    self.request(
                        "PATCH", path, "editor", refused, match=machine["revision"]
                    ),
                    422,
                    "invalid_request",
                )
        changed = self.request(
            "PATCH",
            path,
            "editor",
            dict(name="Renamed", permission_mode="auto"),
            match=machine["revision"],
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        machine = changed.json()
        self.validator.validate(machine, self.schema)
        self.assertEqual(
            (machine["name"], machine["permission_mode"]), ("Renamed", "auto")
        )
        self.assertEqual(machine["host"], "127.0.0.1")
        finished = self.run_approved(machine)
        self.assertEqual(finished["state"], "succeeded", finished)
        # auto: once the code egress is approved, commands run without asking.
        self.assertEqual(self.prepare(machine, "cat demo.txt").json()["state"], "ready")
        listed = subprocess.run(
            ["docker", "exec", self.ssh_container, "ls", "/home/runner/atlas-work"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        self.assertEqual(len(listed), 1, listed)
        self.error(self.request("DELETE", path, "editor"), 428, "precondition_required")
        deleted = self.request("DELETE", path, "editor", match=machine["revision"])
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(
            deleted.json(), dict(id=machine["id"], deleted=True, remote_cleanup=True)
        )
        self.validator.validate(
            deleted.json(),
            self.contract["components"]["schemas"]["PersonalMachineDeleted"],
        )
        # The stored credential, the pinned host and the session state are gone.
        self.assertEqual(self.key_files(), [])
        state = Path(os.environ["ATLAS_RUNNER_STATE"])
        self.assertEqual(list(state.glob("hosts/*/*")), [])
        self.assertEqual(list(state.glob("*/*/*/session.json")), [])
        remaining = subprocess.run(
            ["docker", "exec", self.ssh_container, "ls", "/home/runner/atlas-work"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        self.assertEqual(remaining, [])
        self.error(self.request("GET", path, "editor"), 404, "not_found")
        self.error(self.verify(machine), 404, "not_found")
        self.assertEqual(self.remote("list", {}).json()["items"], [])
        self.assertEqual(self.prepare(machine).status_code, 404)
        with psycopg.connect(self.admin_dsn) as db:
            counts = db.execute(
                "SELECT (SELECT count(*) FROM atlas_projects.runner_targets),"
                "(SELECT count(*) FROM atlas_projects.runner_grants),"
                "(SELECT count(*) FROM atlas_projects.runner_sessions WHERE data->>'state'<>'closed')"
            ).fetchone()
        self.assertEqual(counts, (0, 0, 0))
        # A session record its owner could no longer edit is settled later,
        # without contacting a machine that no longer exists.
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.runner_sessions SET data=jsonb_set(data,'{state}','\"open\"')"
            )
        self.assertEqual(self.remote("close", {}).status_code, 200)
        with psycopg.connect(self.admin_dsn) as db:
            states = db.execute(
                "SELECT data->>'state' FROM atlas_projects.runner_sessions"
            ).fetchall()
        self.assertEqual(states, [("closed",)])
        released = self.turn(
            "release", terminal_state="cancelled", cleanup_confirmed=True
        )
        self.assertEqual(released.status_code, 200, released.text)

    def test_machine_changed_host_key_fails_closed_until_the_owner_resets(self) -> None:
        machine = self.ready()
        real = [fingerprint(self.host_public)]
        self.assertEqual(machine["host_key_fingerprints"], real)
        forged = ["SHA256:" + "A" * 43]
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.runner_targets SET data=jsonb_set(data,'{host_key_fingerprints}',%s) WHERE id=%s",
                (Jsonb(forged), machine["id"]),
            )
        mismatch = self.verify(machine)
        self.assertEqual(mismatch.status_code, 200, mismatch.text)
        self.assertEqual(mismatch.json()["status"], "unavailable")
        self.assertEqual(
            mismatch.json()["last_probe"]["error"], "host_key_fingerprint_mismatch"
        )
        self.assertEqual(mismatch.json()["host_key_fingerprints"], forged)
        self.assertEqual(self.prepare(machine).status_code, 409)
        reset = self.verify(machine, reset_host_key=True)
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(reset.json()["status"], "available", reset.text)
        self.assertEqual(reset.json()["host_key_fingerprints"], real)
        self.error(self.verify(machine, reset_host_key="yes"), 422, "invalid_request")

    def test_machine_internal_address_is_refused_without_operator_listing(self) -> None:
        with patch.dict(os.environ, {"ATLAS_RUNNER_PERSONAL_NETWORKS": ""}):
            declared = self.declare()
            self.assertEqual(declared.status_code, 201, declared.text)
            self.install(declared.json())
            refused = self.verify(declared.json())
        self.assertEqual(refused.status_code, 200, refused.text)
        self.assertEqual(refused.json()["status"], "unverified")
        self.assertEqual(
            refused.json()["last_probe"]["error"], "runner_target_address_denied"
        )
        self.assertEqual(refused.json()["host_key_fingerprints"], [])
        self.assertEqual(self.remote("list", {}).json()["items"], [])


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    # Parent tests are discovered in their own modules; run only this feature's.
    suite = unittest.TestSuite(
        MachineTests(name)
        for name in loader.getTestCaseNames(MachineTests)
        if name.startswith("test_machine_")
    )
    suite.addTests(loader.loadTestsFromTestCase(MachineCommandTests))
    return suite
