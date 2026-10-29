"""REQ-PROJ-05: abandoned Work releases quotas without inventing model usage."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from typing import Any
from uuid import uuid4

import psycopg

from services.retrieval.project_schedule_api import take_due
from services.retrieval.project_scoped_api import store
import test_project_work as fixtures


class WorkLeaseRecoveryTests(fixtures.ProjectWorkTests):
    def prepare_work(self) -> None:
        conversation = self.request(
            "POST",
            self.project_path() + "/conversations",
            "editor",
            dict(title="Recovery"),
        )
        self.assertEqual(conversation.status_code, 201, conversation.text)
        self.conversation = conversation.json()["id"]
        self.session = self.new_session()
        response = self.client.post(
            "/internal/atlas/transcript/begin",
            json=dict(
                request_id=self.turn_id,
                request=dict(
                    model="atlas",
                    org_id=self.org,
                    project_id=self.project,
                    conversation_id=self.conversation,
                    project_action="work",
                    work_session_id=self.session["id"],
                    messages=[dict(role="user", content="Write a report")],
                    stream=True,
                ),
            ),
            headers=self.broker_headers(),
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(
            self.broker(
                "acquire",
                dict(
                    session_id=self.session["id"],
                    request_id=self.turn_id,
                    expected_revision=0,
                    storage_bytes=10,
                ),
            ).status_code,
            200,
        )

    def broker_headers(self) -> dict[str, str]:
        import os
        from services.retrieval.project_identity import sign

        return {
            "X-Atlas-Execution-Secret": os.environ["ATLAS_PROJECT_EXECUTION_SECRET"],
            "X-Atlas-Execution-Audience": "atlas-retrieval-execution",
            "X-Atlas-Project-Identity": sign(
                self.identity("editor", self.project), self.key.encode()
            ),
            "Idempotency-Key": str(uuid4()),
        }

    def new_session(self) -> dict[str, Any]:
        response = self.request(
            "POST",
            self.project_path() + "/work-sessions",
            "editor",
            dict(conversation_id=self.conversation, folder_ids=[], base_versions=[]),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return dict(response.json())

    def expire(self, seconds: int = 60) -> None:
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.work_leases SET expires_at=now()-%s * interval '1 second' WHERE session_id=%s",
                (seconds, self.session["id"]),
            )

    def snapshot(self) -> tuple[Any, ...]:
        with psycopg.connect(self.admin_dsn) as db:
            return (
                db.execute(
                    "SELECT * FROM atlas_projects.work_leases ORDER BY session_id"
                ).fetchall(),
                db.execute(
                    "SELECT * FROM atlas_projects.reservations ORDER BY id"
                ).fetchall(),
                db.execute(
                    "SELECT * FROM atlas_projects.resources ORDER BY id"
                ).fetchall(),
                db.execute("SELECT * FROM atlas_projects.audit ORDER BY id").fetchall(),
            )

    def assert_recovered(self) -> None:
        result = self.request(
            "GET",
            self.project_path() + "/work-sessions/" + self.session["id"],
            "editor",
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["state"], "failed")
        self.assertIsNone(result.json()["lease"])
        turns = self.request(
            "GET",
            self.project_path() + "/conversations/" + self.conversation + "/turns",
            "editor",
        )
        self.assertEqual(turns.status_code, 200, turns.text)
        turn = next(t for t in turns.json()["items"] if t["id"] == self.turn_id)
        self.assertEqual(turn["state"], "failed")
        self.assertIn("interrupted", str(turn["content_refs"]))
        with psycopg.connect(self.admin_dsn) as db:
            rows: dict[str, str] = dict(
                db.execute("SELECT resource,state FROM atlas_projects.reservations")
            )
        for resource in ("concurrent_jobs", "stored_bytes", "temporary_bytes"):
            self.assertEqual(rows[resource], "released")
        for resource in ("daily_model_eur", "monthly_model_eur"):
            self.assertEqual(rows[resource], "unknown")
        self.assertEqual(
            result.json()["usage"]["unknown_eur"],
            self.policy["quotas"]["per_turn"]["cost_eur"],
        )
        self.assertEqual(result.json()["usage"]["reserved_eur"], 0)

    def test_expired_private_lease_frees_organization_slot(self) -> None:
        self.prepare_work()
        self.expire()
        # The reserving member has no access to the abandoned private conversation.
        with store(self.app_dsn).scope(
            self.identity("admin"), self.org, self.second_project
        ) as scope:
            scope.reserve(str(uuid4()), {"concurrent_jobs": 1})
            self.assertEqual(scope.usage("concurrent_jobs"), 1)
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "DELETE FROM atlas_projects.reservations WHERE project_id=%s",
                (self.second_project,),
            )
        self.assert_recovered()
        fresh = self.new_session()
        result = self.broker(
            "acquire",
            dict(
                session_id=fresh["id"],
                request_id=str(uuid4()),
                expected_revision=0,
                storage_bytes=0,
            ),
        )
        self.assertEqual(result.status_code, 200, result.text)

    def test_periodic_recovery_is_idempotent_without_scheduled_tasks(self) -> None:
        self.prepare_work()
        self.expire()
        self.assertEqual(take_due(), {})
        self.assert_recovered()
        before = self.snapshot()
        self.assertEqual(take_due(), {})
        self.assertEqual(self.snapshot(), before)
        self.error(self.turn("renew"), 409, "session_busy")

    def test_renewal_and_grace_are_untouched(self) -> None:
        self.prepare_work()
        self.assertEqual(self.turn("renew").status_code, 200)
        for expired in (False, True):
            if expired:
                self.expire(1)
            before = self.snapshot()
            self.assertEqual(take_due(), {})
            self.assertEqual(self.snapshot(), before)
            fresh = self.new_session()
            result = self.broker(
                "acquire",
                dict(
                    session_id=fresh["id"],
                    request_id=str(uuid4()),
                    expected_revision=0,
                    storage_bytes=0,
                ),
            )
            self.error(result, 429, "quota_exceeded")

    def test_two_database_callers_recover_once(self) -> None:
        self.prepare_work()
        self.expire()
        ready = Barrier(2)

        def recover(_: int) -> int:
            with psycopg.connect(self.app_dsn) as db:
                ready.wait(timeout=5)
                row = db.execute(
                    "SELECT atlas_projects.recover_expired_work_leases(NULL)"
                ).fetchone()
                assert row is not None
                return int(row[0])

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(recover, range(2))), [0, 1])
        self.assert_recovered()

    def test_recovery_rechecks_after_concurrent_renewal_commits(self) -> None:
        self.prepare_work()
        self.expire()
        started = Event()

        def recover() -> int:
            with psycopg.connect(self.app_dsn) as db:
                started.set()
                row = db.execute(
                    "SELECT atlas_projects.recover_expired_work_leases(NULL)"
                ).fetchone()
                assert row is not None
                return int(row[0])

        with ThreadPoolExecutor(max_workers=1) as pool:
            with psycopg.connect(self.admin_dsn) as db:
                db.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (self.org,)
                )
                db.execute(
                    "UPDATE atlas_projects.work_leases SET expires_at=now()+interval '30 seconds'"
                )
                future = pool.submit(recover)
                self.assertTrue(started.wait(5))
            self.assertEqual(future.result(timeout=10), 0)
        self.assertEqual(self.turn("check").status_code, 200)

    def test_revoked_holder_and_known_spend(self) -> None:
        self.prepare_work()
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            for resource in ("daily_model_eur", "monthly_model_eur"):
                scope.reconcile(self.turn_id, resource, 0.02)
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.members SET status='revoked' WHERE id=%s",
                (self.members["editor"],),
            )
        self.expire()
        self.assertEqual(take_due(), {})
        with psycopg.connect(self.admin_dsn) as db:
            rows = db.execute(
                "SELECT amount,state FROM atlas_projects.reservations WHERE resource IN ('daily_model_eur','monthly_model_eur')"
            ).fetchall()
            session = db.execute(
                "SELECT data FROM atlas_projects.resources WHERE id=%s",
                (self.session["id"],),
            ).fetchone()
        self.assertEqual(
            [(float(amount), state) for amount, state in rows],
            [(0.02, "used"), (0.02, "used")],
        )
        assert session is not None
        self.assertEqual(session[0]["state"], "failed")
        self.assertEqual(session[0]["usage"]["cost_eur"], 0.02)
        self.assertEqual(session[0]["usage"]["unknown_eur"], 0)

    def test_lazy_recovery_cannot_select_another_organization(self) -> None:
        self.prepare_work()
        self.expire()
        before = self.snapshot()
        with store(self.app_dsn).scope(
            self.identity("other"), self.other_org, self.foreign_project
        ) as scope:
            scope.db.execute(
                "SELECT atlas_projects.recover_expired_work_leases(%s)",
                (self.other_org,),
            )
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with store(self.app_dsn).scope(
                self.identity("other"), self.other_org, self.foreign_project
            ) as scope:
                scope.db.execute(
                    "SELECT atlas_projects.recover_expired_work_leases(%s)",
                    (self.org,),
                )
        self.assertEqual(self.snapshot(), before)
