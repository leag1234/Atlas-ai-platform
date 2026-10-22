"""REQ-PROJ-05: real scoped database leases, approvals and recovery, without inference."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from services.retrieval.project_identity import sign
from services.retrieval.project_install import upgrade_work
from services.retrieval.project_scoped_api import store
from services.retrieval.project_files import ProjectFiles

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_project_isolation import ProjectIsolationTests
import test_project_auth as auth


class ProjectWorkTests(ProjectIsolationTests):
    def setUp(self) -> None:
        import tempfile

        temporary = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[1] / "BRAIN"
        )
        self.addCleanup(temporary.cleanup)
        paths = patch.dict(
            os.environ,
            {
                "ATLAS_DATA_ROOT": temporary.name + "/blobs",
                "ATLAS_CODE_STATE_DIR": temporary.name + "/state",
            },
        )
        paths.start()
        self.addCleanup(paths.stop)
        super().setUp()
        secret = patch.dict(
            os.environ, {"ATLAS_PROJECT_EXECUTION_SECRET": os.urandom(32).hex()}
        )
        secret.start()
        self.addCleanup(secret.stop)
        self.conversation = str(uuid4())
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "INSERT INTO atlas_projects.legacy_rows VALUES(%s,%s,'conversations',%s,1,%s)",
                (
                    self.org,
                    self.project,
                    self.conversation,
                    Jsonb(dict(id=self.conversation)),
                ),
            )
        response = self.request(
            "POST",
            self.project_path() + "/work-sessions",
            "editor",
            dict(conversation_id=self.conversation, folder_ids=[], base_versions=[]),
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.session = response.json()
        self.validator.validate(
            self.session, self.contract["components"]["schemas"]["WorkSession"]
        )
        self.turn_id = str(uuid4())

    def broker(
        self,
        operation: str,
        data: dict[str, Any],
        role: str = "editor",
        key: str | None = None,
    ) -> Any:
        return self.client.post(
            "/internal/atlas/execution/" + operation,
            json=data,
            headers={
                "X-Atlas-Execution-Secret": os.environ[
                    "ATLAS_PROJECT_EXECUTION_SECRET"
                ],
                "X-Atlas-Execution-Audience": "atlas-retrieval-execution",
                "X-Atlas-Project-Identity": sign(
                    self.identity(role, self.project), self.key.encode()
                ),
                "Idempotency-Key": key or str(uuid4()),
            },
        )

    def acquire(self, **kwargs: Any) -> Any:
        return self.broker(
            "acquire",
            dict(
                session_id=self.session["id"],
                request_id=self.turn_id,
                expected_revision=self.session["revision"],
                storage_bytes=0,
                **kwargs,
            ),
        )

    def fence(self) -> dict[str, Any]:
        return dict(
            session_id=self.session["id"], request_id=self.turn_id, generation=1
        )

    def turn(self, operation: str, **changes: Any) -> Any:
        data = dict(
            self.fence(),
            operation=operation,
            usage=None,
            terminal_state=None,
            cleanup_confirmed=False,
        )
        data.update(changes)
        return self.broker("turn", data)

    def test_concurrent_turns_and_cleanup_boundary(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as workers:
            replies = list(workers.map(lambda _: self.acquire(), range(2)))
        self.assertEqual(sorted(r.status_code for r in replies), [200, 409])
        for reply in replies:
            if reply.status_code == 409:
                self.error(reply, 409, "session_busy")
        self.error(
            self.turn("release", terminal_state="completed"), 422, "invalid_request"
        )
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            self.assertEqual(scope.usage("concurrent_jobs"), 1)
        self.assertEqual(
            self.turn(
                "release", terminal_state="completed", cleanup_confirmed=True
            ).status_code,
            200,
        )
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            self.assertEqual(scope.usage("concurrent_jobs"), 0)
            self.assertEqual(
                scope.usage("monthly_model_eur"),
                self.policy["quotas"]["per_turn"]["cost_eur"],
            )
            row = scope.db.execute(
                "SELECT state FROM atlas_projects.reservations WHERE org_id=%s AND resource='monthly_model_eur'",
                (self.org,),
            ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["state"], "unknown")

    def test_restart_recovery_fences_old_owner_until_cleanup(self) -> None:
        self.assertEqual(self.acquire().status_code, 200)
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.work_leases SET expires_at=now()-interval '1 second'"
            )
        self.error(self.acquire(), 409, "session_busy")
        self.error(self.turn("renew"), 409, "session_busy")
        recovery_id = str(uuid4())
        reply = self.turn("recover", request_id=recovery_id)
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertTrue(reply.json()["cleanup_required"])
        self.assertEqual(reply.json()["session"]["lease"]["generation"], 2)
        self.error(
            self.turn("release", terminal_state="failed", cleanup_confirmed=True),
            409,
            "session_busy",
        )
        self.error(
            self.turn("renew", request_id=recovery_id, generation=2),
            409,
            "session_busy",
        )
        release = self.turn(
            "release",
            request_id=recovery_id,
            generation=2,
            terminal_state="cancelled",
            cleanup_confirmed=True,
        )
        self.assertEqual(release.status_code, 200, release.text)
        self.assertIsNone(release.json()["session"]["lease"])
        self.assertEqual(release.json()["session"]["state"], "cancelled")

    def test_cancel_invalidates_execution_before_slot_release(self) -> None:
        self.assertEqual(self.acquire().status_code, 200)
        result = self.request(
            "POST",
            self.project_path() + "/work-sessions/" + self.session["id"] + "/cancel",
            "owner",
            dict(request_id=self.turn_id),
        )
        self.assertEqual(result.status_code, 202, result.text)
        self.error(self.turn("renew"), 409, "session_busy")
        check = self.turn("check")
        self.assertEqual(check.status_code, 200, check.text)
        self.assertTrue(check.json()["cleanup_required"])
        self.error(self.acquire(), 409, "session_busy")
        result = self.turn(
            "release", terminal_state="cancelled", cleanup_confirmed=True
        )
        self.assertEqual(result.status_code, 200, result.text)

    def issue(self) -> tuple[dict[str, Any], dict[str, Any]]:
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            files = ProjectFiles(scope)
            folder = files.folder(
                dict(name="Inputs", locality="server", model_use="allow")
            )
            file = files.create_file(
                folder["id"], "draft.txt", b"before", folder["revision"]
            )
        data = dict(
            self.fence(),
            approver_member_id=self.members["editor"],
            action_kind="overwrite",
            effects=ProjectFiles.overwrite_effects(file, b"after"),
            destination=dict(kind="server", ref=self.project),
            maximum_bytes=5,
        )
        reply = self.broker("approvals", data)
        self.assertEqual(reply.status_code, 200, reply.text)
        self.validator.validate(
            reply.json(), self.contract["components"]["schemas"]["Approval"]
        )
        return reply.json(), file

    def decision(self, approval: dict[str, Any], **changes: Any) -> Any:
        data = dict(allow=True, action_digest=approval["action_digest"])
        data.update(changes)
        return self.request(
            "POST",
            self.project_path() + "/approvals/" + approval["id"] + "/decision",
            "editor",
            data,
            key="same-decision",
        )

    def test_approval_exact_digest_single_decision_and_transactional_consumption(
        self,
    ) -> None:
        self.assertEqual(self.acquire().status_code, 200)
        approval, file = self.issue()
        self.error(
            self.decision(approval, action_digest="b" * 64), 403, "approval_required"
        )
        decided = self.decision(approval)
        self.assertEqual(decided.status_code, 200, decided.text)
        self.assertIsNone(decided.json()["consumed_at"])
        self.error(self.decision(approval), 410, "approval_expired_or_replayed")
        from services.retrieval.project_identity import ProjectError

        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            with self.assertRaises(ProjectError) as refused:
                ProjectFiles(scope).replace(
                    file["id"], b"wrong", file["version_id"], '"0"', approval["id"]
                )
            self.assertEqual(refused.exception.status, 403)
            updated = ProjectFiles(scope).replace(
                file["id"], b"after", file["version_id"], '"0"', approval["id"]
            )
            self.assertNotEqual(updated["id"], file["version_id"])
            self.assertEqual(ProjectFiles(scope).content(file["id"])[1], b"after")
        self.error(self.decision(approval), 410, "approval_expired_or_replayed")

    def test_approval_expiry_is_audited_and_cannot_be_replayed(self) -> None:
        self.assertEqual(self.acquire().status_code, 200)
        approval, _ = self.issue()
        from datetime import datetime

        lifetime = datetime.fromisoformat(
            approval["expires_at"]
        ) - datetime.fromisoformat(approval["created_at"])
        self.assertLessEqual(abs(lifetime.total_seconds() - 120), 0.1)
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.file_approvals SET data=jsonb_set(data,'{expires_at}',to_jsonb('2020-01-01T00:00:00Z'::text))"
            )
        self.error(self.decision(approval), 410, "approval_expired_or_replayed")
        events = self.request("GET", self.project_path() + "/ledger", "viewer").json()[
            "items"
        ]
        self.assertTrue(
            any((e["audit_record"] or {}).get("outcome") == "expired" for e in events)
        )

    def test_migration_reentrant_and_work_tables_forced_rls(self) -> None:
        upgrade_work(self.admin_dsn)
        upgrade_work(self.admin_dsn)
        with psycopg.connect(self.admin_dsn) as db:
            rows = db.execute(
                "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid IN ('atlas_projects.work_leases'::regclass,'atlas_projects.work_progress'::regclass,'atlas_projects.file_approvals'::regclass)"
            ).fetchall()
        self.assertEqual(rows, [(True, True)] * 3)
        with store(self.app_dsn).scope(
            self.identity("owner"), self.org, self.project
        ) as scope:
            self.assertIsNotNone(
                ProjectFiles(scope).get("work_session", self.session["id"])
            )

    def test_persisted_progress_uses_ledger_sequence_and_reauthorizes_replay(
        self,
    ) -> None:
        import json
        from test_project_isolation import ROOT

        self.assertEqual(self.acquire().status_code, 200)
        event = json.loads(
            (
                ROOT / "contracts/examples/project-mode/ProgressEvent.valid.json"
            ).read_text()
        )
        event.update(
            org_id=self.org,
            project_id=self.project,
            session_id=self.session["id"],
            request_id=self.turn_id,
            approval_id=None,
            job_id=None,
        )
        data = dict(self.fence(), progress=[event], audit=[])
        first = self.broker("events", data, key="same-event")
        self.assertEqual(first.status_code, 200, first.text)
        self.validator.validate(
            first.json(),
            self.contract["components"]["schemas"]["ExecutionEventsResult"],
        )
        repeated = self.broker("events", data, key="same-event")
        self.assertEqual(repeated.json(), first.json())
        seq = first.json()["events"][0]["seq"]
        replay = self.request(
            "GET",
            self.project_path() + f"/events?after={seq - 1}",
            "viewer",
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertIn(f"id: {seq}\n", replay.text)
        self.assertIn("event: atlas.project", replay.text)
        event["org_id"] = self.other_org
        self.error(self.broker("events", data), 404, "not_found")
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.members SET status='revoked',revision=revision+1 WHERE org_id=%s AND id=%s",
                (self.org, self.members["editor"]),
            )
        self.error(self.broker("events", data, key="same-event"), 404, "not_found")
        recovery = dict(
            self.fence(),
            operation="recover",
            usage=None,
            terminal_state=None,
            cleanup_confirmed=False,
            request_id=str(uuid4()),
        )
        recovered = self.broker("turn", recovery, role="owner")
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertTrue(recovered.json()["cleanup_required"])

    def test_approval_and_progress_rls_denies_other_project(self) -> None:
        self.assertEqual(self.acquire().status_code, 200)
        approval, _ = self.issue()
        event = dict(
            org_id=self.org,
            project_id=self.project,
            version=1,
            seq=1,
            session_id=self.session["id"],
            request_id=self.turn_id,
            turn_id=None,
            type="plan",
            status="completed",
            label="Scoped plan",
            tool=None,
            plan=[],
            approval_id=None,
            job_id=None,
            text=None,
            error=None,
            output={},
        )
        persisted = self.broker(
            "events", dict(self.fence(), progress=[event], audit=[])
        )
        self.assertEqual(persisted.status_code, 200, persisted.text)
        with store(self.app_dsn).scope(
            self.identity("admin"), self.org, self.second_project
        ) as scope:
            for table in ("work_leases", "work_progress", "file_approvals"):
                self.assertEqual(
                    scope.db.execute(
                        "SELECT * FROM atlas_projects."
                        + table
                        + " WHERE project_id=%s",
                        (self.project,),
                    ).fetchall(),
                    [],
                )
        self.error(
            self.request(
                "POST",
                self.project_path(self.second_project)
                + "/approvals/"
                + approval["id"]
                + "/decision",
                "admin",
                dict(allow=True, action_digest=approval["action_digest"]),
            ),
            404,
            "not_found",
        )

    def test_recovery_bundle_preserves_work_fences_approvals_and_sequence(self) -> None:
        import json
        import tempfile
        from psycopg import sql
        from psycopg.rows import dict_row
        from scripts import migrate_projects as migration

        self.assertEqual(self.acquire().status_code, 200)
        approval, _ = self.issue()
        with tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[1] / "BRAIN"
        ) as directory:
            bundle = Path(directory)
            with psycopg.connect(self.admin_dsn, row_factory=dict_row) as db:
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
            )
            migration.write_json(
                bundle / "recovery.json", dict(files=migration.inventory(bundle))
            )
            store(self.app_dsn).pool.close()
            with psycopg.connect(self.admin_dsn) as db:
                db.execute("DROP SCHEMA atlas_projects CASCADE")
            restored = migration.restore_database(self.admin_dsn, bundle)
            self.assertEqual(restored["counts"]["work_leases"], 1)
            self.assertEqual(restored["counts"]["file_approvals"], 1)
            saved = json.loads((bundle / "p0-database.json").read_text())
            session = self.request(
                "GET",
                self.project_path() + "/work-sessions/" + self.session["id"],
                "editor",
            ).json()
            self.assertEqual(session["lease"]["generation"], 1)
            self.assertEqual(
                session["pending_approvals"][0]["action_digest"],
                approval["action_digest"],
            )
            self.assertEqual(
                saved["work_leases"][0]["data"]["reservation_id"], self.turn_id
            )
            with psycopg.connect(self.admin_dsn) as db:
                next_seq = db.execute(
                    "SELECT nextval(pg_get_serial_sequence('atlas_projects.ledger_events','seq'))"
                ).fetchone()
            self.assertIsNotNone(next_seq)
            assert next_seq is not None
            self.assertGreater(
                next_seq[0], max(row["seq"] for row in saved["ledger_events"])
            )

    def test_archival_stops_execution_but_allows_fenced_cleanup(self) -> None:
        self.assertEqual(self.acquire().status_code, 200)
        self.issue()
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.projects SET state='archived',revision=revision+1 WHERE org_id=%s AND id=%s",
                (self.org, self.project),
            )
        self.error(self.turn("check"), 403, "policy_denied")
        self.error(self.turn("renew"), 403, "policy_denied")
        recovery_id = str(uuid4())
        recovered = self.turn("recover", request_id=recovery_id)
        self.assertEqual(recovered.status_code, 200, recovered.text)
        released = self.turn(
            "release",
            request_id=recovery_id,
            generation=2,
            terminal_state="cancelled",
            cleanup_confirmed=True,
        )
        self.assertEqual(released.status_code, 200, released.text)
        self.assertIsNone(released.json()["session"]["lease"])

    def test_recreated_grant_cannot_revive_lease_or_approval(self) -> None:
        self.assertEqual(self.acquire().status_code, 200)
        approval, _ = self.issue()
        path = self.project_path() + "/grants/" + self.members["editor"]
        removed = self.request("DELETE", path, "owner", match=1)
        self.assertEqual(removed.status_code, 204, removed.text)
        restored = self.request("PUT", path, "owner", dict(role="editor"), match=0)
        self.assertEqual(restored.status_code, 200, restored.text)
        self.error(self.turn("check"), 403, "policy_denied")
        self.error(self.decision(approval), 410, "approval_expired_or_replayed")


class ProjectWorkHTTPTests(ProjectWorkTests):
    retrieval_url: str
    real_worker_skip_reason: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from services.orchestrator import terminal_stack

        image = os.environ.get("ATLAS_TEST_WORKER_IMAGE", terminal_stack.IMAGE)
        try:
            present = (
                subprocess.run(
                    ["docker", "image", "inspect", image],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                ).returncode
                == 0
            )
            if not present:
                subprocess.run(
                    ["docker", "pull", image],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=300,
                )
                present = (
                    subprocess.run(
                        ["docker", "image", "inspect", image],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    ).returncode
                    == 0
                )
            if not present:
                cls.real_worker_skip_reason = "real worker image unavailable"
        except (OSError, subprocess.TimeoutExpired):
            cls.real_worker_skip_reason = "real worker image unavailable"
        from services.retrieval.api import app

        cls.retrieval_url = getattr(auth.ProjectAuthTests.serve, "__func__")(cls, app)

    def require_real_worker(self) -> None:
        if self.real_worker_skip_reason:
            self.skipTest(self.real_worker_skip_reason)

    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient
        from services.orchestrator.chat_api import app

        self.front_secret = os.urandom(32).hex()
        environment = patch.dict(
            os.environ,
            {
                "ATLAS_PROJECT_MODE": "1",
                "ATLAS_RETRIEVAL_URL": self.retrieval_url,
                "ATLAS_PROJECT_IDENTITY_SECRET": os.urandom(32).hex(),
                "ATLAS_PROJECT_ORIGIN": "http://testserver",
                "ATLAS_PROXY_SECRET": self.front_secret,
                "ATLAS_TRUSTED_EMAIL_HEADER": "X-Atlas-User",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.browser = TestClient(app)
        self.addCleanup(self.browser.close)

    def front(self, subject: str = "editor") -> dict[str, str]:
        return {
            "X-Atlas-User": subject,
            "X-Atlas-Proxy-Secret": self.front_secret,
            "Origin": "http://testserver",
            "Idempotency-Key": str(uuid4()),
        }

    def test_bff_routes_and_real_worker_cancel_cleanup(self) -> None:
        self.require_real_worker()
        import asyncio
        from services.orchestrator.code_api import ExecutionBroker
        from services.orchestrator.code_mode import WorkExecution
        from services.orchestrator.code_workspace import volume
        from services.orchestrator.chat_api import app
        import subprocess

        route = next(
            route
            for route in app.routes
            if getattr(route, "path", "")
            == "/api/atlas/orgs/{org}/projects/{project}/work-sessions"
        )
        catchall = next(
            route
            for route in app.routes
            if getattr(route, "path", "") == "/api/atlas/{path:path}"
        )
        self.assertLess(app.routes.index(route), app.routes.index(catchall))
        session_path = self.project_path() + "/work-sessions/" + self.session["id"]
        self.assertEqual(
            self.browser.get(session_path, headers=self.front()).status_code, 200
        )
        self.assertEqual(
            self.browser.get(session_path, headers=self.front("ungranted")).status_code,
            404,
        )
        broker = ExecutionBroker("editor", self.org, self.project)
        acquired = asyncio.run(
            broker.call(
                "acquire",
                dict(
                    session_id=self.session["id"],
                    request_id=self.turn_id,
                    expected_revision=0,
                    storage_bytes=0,
                ),
            )
        )
        execution = WorkExecution(broker, acquired["session"], self.turn_id)
        workspace = execution.workspace()
        self.addCleanup(
            subprocess.run, ["docker", "volume", "rm", volume()], capture_output=True
        )
        asyncio.run(workspace.prepare())
        try:
            result = asyncio.run(
                workspace.invoke(
                    "write_file",
                    dict(path="proof.txt", content="Scoped work content"),
                    30,
                )
            )
            self.assertNotIn("error", result)
            read = asyncio.run(
                workspace.invoke("read_file", dict(path="proof.txt"), 30)
            )
            self.assertIn("Scoped work content", str(read))

            async def cancel_running() -> Any:
                running = asyncio.create_task(
                    workspace.invoke(
                        "run_command",
                        dict(
                            command="python3 -c 'import time; time.sleep(60)'",
                            timeout_seconds=120,
                            memory_mb=64,
                        ),
                        120,
                    )
                )
                try:
                    for _ in range(100):
                        listing = await asyncio.to_thread(
                            subprocess.run,
                            [
                                "docker",
                                "ps",
                                "-q",
                                "--filter",
                                "label=atlas.work=" + workspace.scope_key,
                            ],
                            capture_output=True,
                            text=True,
                            check=True,
                        )
                        if listing.stdout.strip():
                            break
                        await asyncio.sleep(0.05)
                    else:
                        self.fail("The scoped worker did not start")
                    reply = await asyncio.to_thread(
                        self.browser.post,
                        session_path + "/cancel",
                        headers=self.front(),
                        json=dict(request_id=self.turn_id),
                    )
                    stopped = await asyncio.gather(running, return_exceptions=True)
                    self.assertIsInstance(stopped[0], RuntimeError)
                    return reply
                finally:
                    if not running.done():
                        running.cancel()
                        await asyncio.gather(running, return_exceptions=True)

            cancelled = asyncio.run(asyncio.wait_for(cancel_running(), 30))
            self.assertEqual(cancelled.status_code, 202, cancelled.text)
            after = self.browser.get(session_path, headers=self.front()).json()
            self.assertEqual(after["state"], "cancelled")
            self.assertIsNone(after["lease"])
            self.assertFalse(workspace.metadata_path.parent.exists())
            self.assertEqual(
                subprocess.run(
                    [
                        "docker",
                        "ps",
                        "-aq",
                        "--filter",
                        "label=atlas.work=" + workspace.scope_key,
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip(),
                "",
            )
        finally:
            asyncio.run(workspace.cleanup())

    def test_work_handoff_runs_tools_and_releases_after_cleanup(self) -> None:
        self.require_real_worker()
        import asyncio
        from decimal import Decimal
        from unittest.mock import AsyncMock, MagicMock
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.code_mode import CodeModel, process_work
        from services.orchestrator.loop import CodeResult, Call
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.code_workspace import volume
        import subprocess

        self.policy["quotas"]["per_turn"]["expanded_bytes"] = 50
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.installation SET ceilings=%s",
                (Jsonb(self.policy),),
            )
            db.execute(
                "UPDATE atlas_projects.organizations SET policy=%s",
                (Jsonb(self.policy),),
            )
        # Refresh the persisted displayed limits before acquisition; the broker
        # rechecks current ceilings at acquisition independently.
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            files = ProjectFiles(scope)
            value = files.get("work_session", self.session["id"])
            value["effective_limits"] = self.policy["quotas"]["per_turn"]
            files.save(value)
        request = ChatRequest.model_validate(
            dict(
                messages=[dict(role="user", content="Create a note")],
                project_id=self.project,
                conversation_id=self.conversation,
            )
        )
        item = Interaction()
        model = MagicMock(spec=CodeModel)
        model.spent, model.input_tokens, model.output_tokens, model.work_unknown = (
            Decimal(0),
            0,
            0,
            False,
        )

        async def turn(*args: Any) -> CodeResult:
            tools = args[2]
            written = await tools.execute(
                Call(
                    "write",
                    "write_file",
                    '{"path":"note.txt","content":"A useful note"}',
                ),
                30,
            )
            self.assertNotIn("error", written)
            read = await tools.execute(
                Call("read", "read_file", '{"path":"note.txt"}'), 30
            )
            self.assertIn("A useful note", str(read))
            return CodeResult(
                text="Created the requested note in the workspace.",
                state="done",
                tool_calls=2,
                model_calls=0,
            )

        self.addCleanup(
            subprocess.run, ["docker", "volume", "rm", volume()], capture_output=True
        )
        with (
            patch.object(CodeModel, "connect", AsyncMock(return_value=model)),
            patch("services.orchestrator.code_mode.execute_turn", side_effect=turn),
        ):
            asyncio.run(
                process_work(
                    request,
                    item,
                    subject="editor",
                    org_id=self.org,
                    project_id=self.project,
                    session_id=self.session["id"],
                )
            )
        self.assertEqual(item.state, "done")
        session = self.request(
            "GET",
            self.project_path() + "/work-sessions/" + self.session["id"],
            "editor",
        ).json()
        self.assertIsNone(session["lease"])
        self.assertEqual(session["state"], "completed")
        self.assertEqual(session["usage"]["tool_calls"], 2)
        self.assertGreater(session["last_event_seq"], 0)
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            self.assertEqual(scope.usage("concurrent_jobs"), 0)
            self.assertEqual(scope.usage("temporary_bytes"), 0)
            self.assertEqual(scope.usage("monthly_model_eur"), 0)

    def test_restart_cleanup_failure_retains_reservations(self) -> None:
        self.require_real_worker()
        import asyncio
        from unittest.mock import AsyncMock
        from services.orchestrator.code_api import ExecutionBroker
        from services.orchestrator.code_mode import cleanup_work
        from services.orchestrator.code_workspace import Workspace

        self.assertEqual(self.acquire().status_code, 200)
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.work_leases SET expires_at=now()-interval '1 second'"
            )
        broker = ExecutionBroker("editor", self.org, self.project)
        with patch.object(
            Workspace, "cleanup", AsyncMock(side_effect=RuntimeError("cleanup failed"))
        ):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                asyncio.run(cleanup_work(broker, self.session["id"]))
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            self.assertEqual(scope.usage("concurrent_jobs"), 1)
            self.assertEqual(
                scope.usage("monthly_model_eur"),
                self.policy["quotas"]["per_turn"]["cost_eur"],
            )
        session = self.request(
            "GET",
            self.project_path() + "/work-sessions/" + self.session["id"],
            "editor",
        ).json()
        self.assertEqual(session["state"], "cancelling")
        self.assertEqual(session["lease"]["generation"], 2)

        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.work_leases SET expires_at=now()-interval '1 second'"
            )
        asyncio.run(
            cleanup_work(
                ExecutionBroker("editor", self.org, self.project), self.session["id"]
            )
        )
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            self.assertEqual(scope.usage("concurrent_jobs"), 0)
            self.assertEqual(
                scope.usage("monthly_model_eur"),
                self.policy["quotas"]["per_turn"]["cost_eur"],
            )
