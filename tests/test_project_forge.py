"""REQ-PROJ-05: real local Git HTTP + fake GitHub PR service, no external calls."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import socket
import sys
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import psycopg
from psycopg.types.json import Jsonb

from services.orchestrator.project_forge import Connector, GitHubForge, canonical
from services.retrieval.project_files import ProjectFiles
from services.retrieval.project_forge import RepositoryActions, run_operation
from services.retrieval.project_scoped_api import store

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_project_isolation as isolation

Json = dict[str, Any]


class LocalForge:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "demo.git"
        self.repo.mkdir()
        self.git("init", "--bare", ".")
        self.git("config", "http.receivepack", "true")
        tree = self.git("mktree", content=b"")
        self.base = self.git("commit-tree", tree, content=b"Baseline\n")
        self.git("update-ref", "refs/heads/main", self.base)
        self.pull_requests: list[Json] = []
        self.drop_next = False
        self.reject_reads = False
        self.writes = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                self.handle_request()

            def do_POST(self) -> None:
                self.handle_request()

            def handle_request(self) -> None:
                content = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                parsed = urlsplit(self.path)
                if outer.reject_reads and self.command == "GET":
                    self.send_error(503)
                    return
                effect = False
                if parsed.path.startswith("/repos/"):
                    if self.command == "POST":
                        value = json.loads(content)
                        outer.pull_requests.append(
                            dict(
                                title=value["title"],
                                body=value["body"],
                                head=dict(
                                    ref=value["head"],
                                    sha=outer.git(
                                        "rev-parse", "refs/heads/" + value["head"]
                                    ),
                                ),
                                base=dict(
                                    ref=value["base"],
                                    sha=outer.git(
                                        "rev-parse", "refs/heads/" + value["base"]
                                    ),
                                ),
                            )
                        )
                        effect = True
                    payload = json.dumps(
                        outer.pull_requests
                        if self.command == "GET"
                        else outer.pull_requests[-1]
                    ).encode()
                    response_headers = {"Content-Type": "application/json"}
                else:
                    response = subprocess.run(
                        ["git", "http-backend"],
                        input=content,
                        capture_output=True,
                        check=True,
                        env={
                            "PATH": os.defpath,
                            "GIT_PROJECT_ROOT": str(root),
                            "GIT_HTTP_EXPORT_ALL": "1",
                            "REQUEST_METHOD": self.command,
                            "PATH_INFO": parsed.path,
                            "QUERY_STRING": parsed.query,
                            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                            "CONTENT_LENGTH": str(len(content)),
                            "REMOTE_USER": "test",
                            "REMOTE_ADDR": "127.0.0.1",
                        },
                    ).stdout
                    head, payload = response.split(b"\r\n\r\n", 1)
                    response_headers = dict(
                        line.decode().split(": ", 1) for line in head.split(b"\r\n")
                    )
                    effect = (
                        parsed.path.endswith("git-receive-pack")
                        and self.command == "POST"
                    )
                if effect:
                    outer.writes += 1
                    if outer.drop_next:
                        outer.drop_next = False
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                self.send_response(
                    int(response_headers.pop("Status", "200").split()[0])
                )
                for name, value in response_headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def git(self, *args: str, content: bytes | None = None) -> str:
        return (
            subprocess.run(
                ["git", *args],
                cwd=self.repo,
                input=content,
                capture_output=True,
                check=True,
                env={
                    "PATH": os.defpath,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "test@example.invalid",
                },
            )
            .stdout.decode()
            .strip()
        )

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class LoopbackTransport(httpx.BaseTransport):
    def __init__(self, url: str) -> None:
        self.url = url

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        with httpx.Client(trust_env=False) as client:
            reply = client.request(
                request.method,
                self.url + request.url.raw_path.decode(),
                headers=request.headers,
                content=request.read(),
            )
            return httpx.Response(
                reply.status_code, headers=reply.headers, content=reply.content
            )


class LocalGitHub(GitHubForge):
    """Only tests redirect the explicitly configured HTTPS connector to loopback."""

    def __init__(self, remote: LocalForge, token: str) -> None:
        super().__init__(
            Connector(
                "https://forge.example/demo.git",
                "https://forge.example",
                "org/demo",
                "Test",
                "test@example.invalid",
                token,
            ),
            transport=LoopbackTransport(remote.url),
        )
        self.remote = remote

    def environment(self) -> dict[str, str]:
        result = super().environment()
        result.update(
            GIT_CONFIG_COUNT="7",
            GIT_CONFIG_KEY_5="protocol.http.allow",
            GIT_CONFIG_VALUE_5="always",
            GIT_CONFIG_KEY_6="url." + self.remote.url + "/.insteadOf",
            GIT_CONFIG_VALUE_6="https://forge.example/",
        )
        return result


class ProjectForgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        isolation.ProjectIsolationTests.setUpClass()
        cls.addClassCleanup(isolation.ProjectIsolationTests.doClassCleanups)

    def setUp(self) -> None:
        self.case = isolation.ProjectIsolationTests()
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        root = Path(self.case.directory.name)
        env = patch.dict(
            os.environ,
            {
                "ATLAS_DATA_ROOT": str(root / "blobs"),
                "ATLAS_FORGE_SCRATCH_ROOT": str(root / "scratch"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.remote = LocalForge(root)
        self.addCleanup(self.remote.close)
        self.token = os.urandom(24).hex()
        self.adapter = LocalGitHub(self.remote, self.token)
        factory = patch(
            "services.retrieval.project_forge.configured_adapter",
            return_value=self.adapter,
        )
        factory.start()
        self.addCleanup(factory.stop)
        self.connector = str(uuid4())
        # The inherited fixture intentionally has a tiny upload quota. Raise only
        # this fixture's operator ceiling so a snapshot can be tested honestly.
        with psycopg.connect(self.case.admin_dsn) as db:
            for table, column in (
                ("installation", "ceilings"),
                ("organizations", "policy"),
            ):
                db.execute(
                    f"UPDATE atlas_projects.{table} SET {column}=jsonb_set(jsonb_set(jsonb_set(jsonb_set({column},'{{quotas,upload_bytes}}','100000'),'{{quotas,stored_bytes}}','100000'),'{{quotas,versioned_bytes}}','100000'),'{{connector_allowlist}}',%s)",
                    (Jsonb([self.connector]),),
                )
        with store(self.case.app_dsn).scope(
            self.case.identity("editor"), self.case.org, self.case.project
        ) as scope:
            files = ProjectFiles(scope)
            folder = files.folder(
                dict(name="Source", locality="server", model_use="allow")
            )
            self.repository = dict(
                files.new(),
                folder_id=folder["id"],
                remote_ref="https://forge.example/demo.git",
                base_commit=self.remote.base,
                forge="github",
                connector_id=self.connector,
            )
            files.put("repository", self.repository)
            self.content = canonical(
                dict(
                    files=[
                        dict(
                            path="hello.txt",
                            mode="100644",
                            content=base64.b64encode(b"Approved bytes\n").decode(),
                        )
                    ]
                )
            )
            self.file = files.create_file(
                folder["id"], "snapshot.json", self.content, folder["revision"]
            )
        self.path = (
            self.case.project_path()
            + "/repositories/"
            + self.repository["id"]
            + "/actions"
        )

    def action(self, kind: str) -> Json:
        params = {
            "branch": dict(name="review/topic"),
            "commit": dict(message="Reviewed change"),
            "pull_request": dict(
                title="Review", body="Please review", head="topic", target="main"
            ),
        }[kind]
        if kind == "pull_request":
            self.remote.git("update-ref", "refs/heads/topic", self.remote.base)
        value = dict(
            kind=kind,
            base_commit=self.remote.base,
            change_digest="a" * 64,
            parameters=params,
        )
        if kind == "commit":
            value.update(
                target_branch="main",
                snapshot=dict(
                    id=self.file["version_id"],
                    sha256=hashlib.sha256(self.content).hexdigest(),
                ),
            )
        return value

    def pending(self, action: Json) -> Json:
        response = self.case.request("POST", self.path, "editor", action)
        self.assertEqual(response.status_code, 202, response.text)
        result: Json = response.json()
        self.case.validator.validate(
            result, self.case.contract["components"]["schemas"]["Operation"]
        )
        self.assertEqual(result["state"], "pending")
        return result

    def allow(self, pending: Json) -> Any:
        return self.case.request(
            "POST",
            self.case.project_path()
            + "/approvals/"
            + pending["approval_id"]
            + "/decision",
            "editor",
            dict(allow=True, action_digest=pending["approval"]["action_digest"]),
        )

    def stored(self, pending: Json) -> Json:
        with store(self.case.app_dsn).scope(
            self.case.identity("editor"), self.case.org, self.case.project
        ) as scope:
            return RepositoryActions(scope).operation(pending["operation_id"])

    def test_each_action_requires_exact_approval_and_refuses_replay(self) -> None:
        for kind in ("branch", "commit", "pull_request"):
            with self.subTest(kind=kind):
                action = self.action(kind)
                if kind == "pull_request":
                    action["base_commit"] = self.remote.git(
                        "rev-parse", "refs/heads/main"
                    )
                pending = self.pending(action)
                before = self.remote.writes
                unapproved = dict(action, approval_id=pending["approval_id"])
                self.case.error(
                    self.case.request("POST", self.path, "editor", unapproved),
                    403,
                    "approval_required",
                )
                decision = (
                    self.case.project_path()
                    + "/approvals/"
                    + pending["approval_id"]
                    + "/decision"
                )
                self.case.error(
                    self.case.request(
                        "POST",
                        decision,
                        "editor",
                        dict(allow=True, action_digest="0" * 64),
                    ),
                    403,
                    "approval_required",
                )
                self.assertEqual(self.remote.writes, before)
                self.assertEqual(self.allow(pending).status_code, 200)
                changed = copy.deepcopy(unapproved)
                changed["change_digest"] = "f" * 64
                self.case.error(
                    self.case.request("POST", self.path, "editor", changed),
                    403,
                    "approval_required",
                )
                send_key = str(uuid4())
                sent = self.case.request(
                    "POST", self.path, "editor", unapproved, key=send_key
                )
                self.assertEqual(sent.status_code, 202, sent.text)
                self.assertEqual(self.stored(pending)["state"], "succeeded")
                self.case.error(
                    self.case.request(
                        "POST", self.path, "editor", unapproved, key=send_key
                    ),
                    410,
                    "approval_expired_or_replayed",
                )
                self.case.error(
                    self.case.request("POST", self.path, "editor", unapproved),
                    410,
                    "approval_expired_or_replayed",
                )
                self.case.error(
                    self.allow(pending), 410, "approval_expired_or_replayed"
                )
                self.assertEqual(self.remote.writes, before + 1)
        self.assertEqual(self.remote.git("show", "main:hello.txt"), "Approved bytes")
        self.assertIn(
            "Atlas-Operation:", self.remote.git("log", "-1", "--format=%B", "main")
        )

    def test_timeout_after_each_remote_write_reconciles_after_restart_without_duplicates(
        self,
    ) -> None:
        for kind in ("branch", "commit", "pull_request"):
            with self.subTest(kind=kind):
                action = self.action(kind)
                if kind == "pull_request":
                    action["base_commit"] = self.remote.git("rev-parse", "main")
                pending = self.pending(action)
                self.assertEqual(self.allow(pending).status_code, 200)
                self.remote.drop_next = True
                result = self.case.request(
                    "POST",
                    self.path,
                    "editor",
                    dict(action, approval_id=pending["approval_id"]),
                )
                self.assertEqual(result.status_code, 202, result.text)
                self.assertEqual(self.stored(pending)["state"], "unknown")
                writes = self.remote.writes
                store(self.case.app_dsn).pool.close()
                run_operation(
                    store(self.case.app_dsn),
                    self.case.identity("editor", self.case.project),
                    pending["operation_id"],
                )
                self.assertEqual(self.stored(pending)["state"], "succeeded")
                self.assertEqual(self.remote.writes, writes)

    def test_snapshot_and_base_changes_refused_without_effects(self) -> None:
        action = self.action("commit")
        pending = self.pending(action)
        self.assertEqual(self.allow(pending).status_code, 200)
        for mutate in ("message", "snapshot", "branch", "base"):
            changed = copy.deepcopy(action)
            if mutate == "message":
                changed["parameters"]["message"] = "Different"
            elif mutate == "snapshot":
                changed["snapshot"]["sha256"] = "b" * 64
            elif mutate == "branch":
                changed["target_branch"] = "other"
            else:
                changed["base_commit"] = "b" * 40
            self.case.error(
                self.case.request(
                    "POST",
                    self.path,
                    "editor",
                    dict(changed, approval_id=pending["approval_id"]),
                ),
                403,
                "approval_required",
            )
        tree = self.remote.git("rev-parse", "main^{tree}")
        moved = self.remote.git(
            "commit-tree", tree, "-p", self.remote.base, content=b"Concurrent\n"
        )
        self.remote.git("update-ref", "refs/heads/main", moved)
        self.case.error(
            self.case.request(
                "POST",
                self.path,
                "editor",
                dict(action, approval_id=pending["approval_id"]),
            ),
            409,
            "revision_conflict",
        )
        self.assertEqual(self.remote.writes, 0)

    def test_credentials_isolated_and_rls_enforced(self) -> None:
        pending = self.pending(self.action("branch"))
        self.assertNotIn(self.token, repr(self.adapter.config))
        self.assertNotIn(self.token, json.dumps(pending))
        with store(self.case.app_dsn).scope(
            self.case.identity("editor"), self.case.org, self.case.project
        ) as scope:
            rows = scope.db.execute(
                "SELECT data FROM atlas_projects.ledger_events"
            ).fetchall()
            self.assertNotIn(self.token, json.dumps(rows))
        with store(self.case.app_dsn).scope(
            self.case.identity("other"), self.case.other_org, self.case.foreign_project
        ) as scope:
            row = scope.db.execute(
                "SELECT count(*) AS n FROM atlas_projects.forge_operations"
            ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["n"], 0)
        with psycopg.connect(self.case.admin_dsn) as db:
            self.assertEqual(
                db.execute(
                    "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid='atlas_projects.forge_operations'::regclass"
                ).fetchone(),
                (True, True),
            )
        self.case.error(
            self.case.request("POST", self.path, "viewer", self.action("branch")),
            403,
            "permission_denied",
        )
        self.case.error(
            self.case.request("POST", self.path, "other", self.action("branch")),
            404,
            "not_found",
        )

    def test_failed_reconciliation_does_not_retry(self) -> None:
        action = self.action("branch")
        pending = self.pending(action)
        self.assertEqual(self.allow(pending).status_code, 200)
        self.remote.drop_next = True
        self.case.request(
            "POST",
            self.path,
            "editor",
            dict(action, approval_id=pending["approval_id"]),
        )
        self.remote.reject_reads = True
        run_operation(
            store(self.case.app_dsn),
            self.case.identity("editor", self.case.project),
            pending["operation_id"],
        )
        self.assertEqual(self.stored(pending)["state"], "unknown")
        self.assertEqual(self.remote.writes, 1)

    def test_expiry_and_corrupt_snapshot_cannot_publish(self) -> None:
        action = self.action("commit")
        pending = self.pending(action)
        self.assertEqual(self.allow(pending).status_code, 200)
        with store(self.case.app_dsn).scope(
            self.case.identity("editor"), self.case.org, self.case.project
        ) as scope:
            broker = RepositoryActions(scope)
            approval = broker.work.approval(pending["approval_id"])["data"]
            approval["expires_at"] = "2020-01-01T00:00:00Z"
            broker.work.update_approval(approval)
        self.case.error(
            self.case.request(
                "POST",
                self.path,
                "editor",
                dict(action, approval_id=pending["approval_id"]),
            ),
            410,
            "approval_expired_or_replayed",
        )
        with store(self.case.app_dsn).scope(
            self.case.identity("editor"), self.case.org, self.case.project
        ) as scope:
            row = scope.db.execute(
                "SELECT data FROM atlas_projects.ledger_events WHERE data->'audit_record'->>'approval_id'=%s AND data->'audit_record'->>'outcome'='expired'",
                (pending["approval_id"],),
            ).fetchone()
            self.assertIsNotNone(row)
        pending = self.pending(action)
        self.assertEqual(self.allow(pending).status_code, 200)
        with store(self.case.app_dsn).scope(
            self.case.identity("editor"), self.case.org, self.case.project
        ) as scope:
            ProjectFiles(scope).blob_path(
                self.file["id"], self.file["version_id"]
            ).write_bytes(b"corrupted")
        self.case.error(
            self.case.request(
                "POST",
                self.path,
                "editor",
                dict(action, approval_id=pending["approval_id"]),
            ),
            503,
            "dependency_unavailable",
        )
        self.assertEqual(self.remote.writes, 0)

    def test_pr_head_change_and_connector_change_require_new_approval(self) -> None:
        action = self.action("pull_request")
        pending = self.pending(action)
        self.assertEqual(self.allow(pending).status_code, 200)
        tree = self.remote.git("rev-parse", "main^{tree}")
        head = self.remote.git(
            "commit-tree", tree, "-p", self.remote.base, content=b"Changed head\n"
        )
        self.remote.git("update-ref", "refs/heads/topic", head)
        self.case.error(
            self.case.request(
                "POST",
                self.path,
                "editor",
                dict(action, approval_id=pending["approval_id"]),
            ),
            409,
            "revision_conflict",
        )
        action = self.action("branch")
        pending = self.pending(action)
        self.assertEqual(self.allow(pending).status_code, 200)
        from dataclasses import replace

        self.adapter.config = replace(self.adapter.config, author_name="Changed author")
        self.case.error(
            self.case.request(
                "POST",
                self.path,
                "editor",
                dict(action, approval_id=pending["approval_id"]),
            ),
            409,
            "revision_conflict",
        )
        self.assertEqual(self.remote.writes, 0)

    def test_worker_command_does_not_inherit_broker_credentials(self) -> None:
        from services.orchestrator.code_worker import command

        with patch.dict(os.environ, {"ATLAS_FORGE_TOKEN_TEST": self.token}):
            output = command(
                Path(self.case.directory.name),
                'test -z "$ATLAS_FORGE_TOKEN_TEST" && echo isolated',
                2,
                64,
            )
        self.assertIn("isolated", str(output))
        self.assertNotIn(self.token, str(output))

    def test_git_compare_and_swap_refuses_a_race_after_base_read(self) -> None:
        from services.orchestrator.project_forge import ForgeUnknown

        action = self.action("commit")
        pending = self.pending(action)
        operation = self.stored(pending)
        operation["commit"] = self.adapter.prepare(operation, self.content)
        tree = self.remote.git("rev-parse", "main^{tree}")
        concurrent = self.remote.git(
            "commit-tree", tree, "-p", self.remote.base, content=b"Concurrent edit\n"
        )
        original = self.adapter.git

        def move_before_push(args: list[str], *values: Any, **kwargs: Any) -> bytes:
            if args[0] == "push":
                self.remote.git("update-ref", "refs/heads/main", concurrent)
            return original(args, *values, **kwargs)

        with patch.object(self.adapter, "git", side_effect=move_before_push):
            with self.assertRaises(ForgeUnknown):
                self.adapter.send(operation, self.content)
        self.assertEqual(self.remote.git("rev-parse", "main"), concurrent)
        self.assertEqual(self.adapter.reconcile(operation), "unknown")

    def test_repeatable_migration_and_snapshot_validation(self) -> None:
        from services.retrieval.project_install import upgrade_work
        from services.orchestrator.project_forge import ForgeError, snapshot_files

        upgrade_work(self.case.admin_dsn)
        self.assertEqual(
            len(snapshot_files(self.content, hashlib.sha256(self.content).hexdigest())),
            1,
        )
        for paths in (
            ["../outside"],
            [".git/config"],
            ["a", "a/b"],
            ["a", "a-b", "a/c"],
            ["b", "a"],
            ["duplicate", "duplicate"],
        ):
            payload = canonical(
                dict(
                    files=[dict(path=path, mode="100644", content="") for path in paths]
                )
            )
            with self.assertRaises(ForgeError):
                snapshot_files(payload, hashlib.sha256(payload).hexdigest())

    def test_revoked_grant_blocks_an_already_approved_action(self) -> None:
        action = self.action("branch")
        pending = self.pending(action)
        self.assertEqual(self.allow(pending).status_code, 200)
        response = self.case.request(
            "DELETE",
            self.case.project_path() + "/grants/" + self.case.members["editor"],
            "owner",
            match=1,
        )
        self.assertEqual(response.status_code, 204, response.text)
        self.case.error(
            self.case.request(
                "POST",
                self.path,
                "editor",
                dict(action, approval_id=pending["approval_id"]),
            ),
            404,
            "not_found",
        )
        self.assertEqual(self.remote.writes, 0)

    def test_concurrent_recovery_sends_once(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        action = self.action("branch")
        pending = self.pending(action)
        self.assertEqual(self.allow(pending).status_code, 200)
        with patch("services.retrieval.project_scoped_api.run_operation"):
            response = self.case.request(
                "POST",
                self.path,
                "editor",
                dict(action, approval_id=pending["approval_id"]),
            )
        self.assertEqual(response.status_code, 202, response.text)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    run_operation,
                    store(self.case.app_dsn),
                    self.case.identity("editor", self.case.project),
                    pending["operation_id"],
                )
                for _ in range(2)
            ]
            for future in futures:
                future.result()
        self.assertEqual(self.stored(pending)["state"], "succeeded")
        self.assertEqual(self.remote.writes, 1)

    def test_database_restore_preserves_unknown_operation_and_prevents_duplicate(
        self,
    ) -> None:
        from psycopg import sql
        from psycopg.rows import dict_row
        from scripts import migrate_projects as migration

        action = self.action("branch")
        pending = self.pending(action)
        self.assertEqual(self.allow(pending).status_code, 200)
        self.remote.drop_next = True
        response = self.case.request(
            "POST",
            self.path,
            "editor",
            dict(action, approval_id=pending["approval_id"]),
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.stored(pending)["state"], "unknown")
        bundle = Path(self.case.directory.name) / "recovery"
        bundle.mkdir()
        with psycopg.connect(self.case.admin_dsn, row_factory=dict_row) as db:
            tables = db.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='atlas_projects'"
            ).fetchall()
            exported = {
                row["tablename"]: list(
                    db.execute(
                        sql.SQL("SELECT * FROM atlas_projects.{}").format(
                            sql.Identifier(row["tablename"])
                        )
                    )
                )
                for row in tables
            }
        migration.write_json(bundle / "p0-database.json", exported)
        schema_root = Path(__file__).resolve().parents[1] / "services/retrieval"
        (bundle / "schema.sql").write_text(
            (schema_root / "project_schema.sql").read_text()
            + "\n"
            + (schema_root / "project_work_schema.sql").read_text()
            + "\n"
            + (schema_root / "project_transcript_schema.sql").read_text()
        )
        migration.write_json(
            bundle / "recovery.json", dict(files=migration.inventory(bundle))
        )
        store(self.case.app_dsn).pool.close()
        with psycopg.connect(self.case.admin_dsn) as db:
            db.execute("DROP SCHEMA atlas_projects CASCADE")
        result = migration.restore_database(self.case.admin_dsn, bundle)
        self.assertEqual(result["counts"]["forge_operations"], 1)
        run_operation(
            store(self.case.app_dsn),
            self.case.identity("editor", self.case.project),
            pending["operation_id"],
        )
        self.assertEqual(self.stored(pending)["state"], "succeeded")
        self.assertEqual(self.remote.writes, 1)
        self.case.error(
            self.case.request(
                "POST",
                self.path,
                "editor",
                dict(action, approval_id=pending["approval_id"]),
            ),
            410,
            "approval_expired_or_replayed",
        )
