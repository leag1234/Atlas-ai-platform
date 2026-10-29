"""Scheduled task database claims, isolation, quotas and revocation; fictional data."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import psycopg

from services.retrieval.project_scoped_api import store
from services.retrieval.project_schedules import Schedules, clock
from services.retrieval.project_schedule_api import take_due
from services.retrieval.project_identity import ProjectError
from test_project_isolation import ProjectIsolationTests


class ScheduledTaskTests(ProjectIsolationTests):
    def setUp(self) -> None:
        import os
        import tempfile

        root = tempfile.TemporaryDirectory(dir="BRAIN")
        self.addCleanup(root.cleanup)
        environment = patch.dict(os.environ, {"ATLAS_DATA_ROOT": root.name})
        environment.start()
        self.addCleanup(environment.stop)
        super().setUp()

    def payload(self) -> dict[str, Any]:
        return dict(
            name="Fictional report",
            instruction="Write a fictional orchard summary.",
            schedule=dict(
                frequency="every", days=3, time="09:00", timezone="Europe/Paris"
            ),
            target_file="Orchard.docx",
        )

    def create_task(self) -> dict[str, Any]:
        response = self.request(
            "POST", self.project_path() + "/scheduled-tasks", "editor", self.payload()
        )
        self.assertEqual(response.status_code, 201, response.text)
        return dict(response.json())

    def due(self, task: dict[str, Any]) -> None:
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.scheduled_tasks SET next_run=now()-interval '10 days' WHERE id=%s",
                (task["id"],),
            )

    def test_roles_and_cross_project(self) -> None:
        task = self.create_task()
        path = self.project_path() + "/scheduled-tasks"
        self.assertEqual(
            self.request("GET", path, "viewer").json()["items"][0]["id"], task["id"]
        )
        self.assertEqual(
            self.request("POST", path, "viewer", self.payload()).status_code, 403
        )
        self.assertEqual(
            self.request(
                "POST", path + "/" + task["id"] + "/run", "viewer", {}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.request(
                "GET",
                self.project_path(self.second_project)
                + "/scheduled-tasks/"
                + task["id"],
                "admin",
            ).status_code,
            404,
        )
        result = self.request(
            "PUT",
            path + "/" + task["id"],
            "owner",
            dict(self.payload(), enabled=False),
            match=task["revision"],
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()["enabled"])

    def test_claim_once_and_missed_slots(self) -> None:
        task = self.create_task()
        self.due(task)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: take_due(), range(2)))
        jobs = [r for r in results if r]
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertGreater(job["task"]["next_run"], clock().isoformat())
        self.assertEqual(take_due(), {})
        conversations = self.request(
            "GET", self.project_path() + "/conversations", "viewer"
        ).json()["items"]
        self.assertEqual(len(conversations), 1)
        self.assertTrue(conversations[0]["title"].startswith("Fictional report - "))
        self.assertEqual(conversations[0]["id"], job["conversation_id"])

    def test_three_failures_pause_and_budget_is_reserved(self) -> None:
        task = self.create_task()
        for index in range(3):
            self.due(task)
            # A new local day grants a fresh daily cap, independently of failures.
            with store(self.app_dsn).scope(
                self.identity("editor"), self.org, self.project
            ) as scope:
                schedules = Schedules(scope)
                row = schedules.row(task["id"])
                row["data"]["spent_day"] = None
                schedules.save(row)
            job = take_due()
            self.assertTrue(job)
            with store(self.app_dsn).scope(
                self.identity("editor"), self.org, self.project
            ) as scope:
                value = Schedules(scope).finish(task["id"], job["run_id"], "failed", 0)
                self.assertEqual(value["failures"], index + 1)
        self.assertFalse(value["enabled"])
        self.assertEqual(value["pause_reason"], "three_failures")
        self.due(task)
        self.assertEqual(take_due(), {})

    def test_task_cap_prevents_another_run(self) -> None:
        task = self.create_task()
        self.due(task)
        job = take_due()
        self.assertEqual(job["reserved_eur"], 0.5)
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            Schedules(scope).finish(task["id"], job["run_id"], "failed", 0)
        self.due(task)
        self.assertEqual(take_due(), {})
        result = self.request(
            "GET", self.project_path() + "/scheduled-tasks/" + task["id"], "viewer"
        ).json()
        self.assertEqual(result["last_result"]["status"], "budget_cap")

    def test_removed_membership_pauses(self) -> None:
        task = self.create_task()
        self.due(task)
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "DELETE FROM atlas_projects.grants WHERE org_id=%s AND project_id=%s AND member_id=%s",
                (self.org, self.project, self.members["editor"]),
            )
        self.assertEqual(take_due(), {})
        value = self.request(
            "GET", self.project_path() + "/scheduled-tasks/" + task["id"], "owner"
        ).json()
        self.assertFalse(value["enabled"])
        self.assertEqual(value["pause_reason"], "access_lost")

    def test_expiry_fences_old_execution(self) -> None:
        task = self.create_task()
        self.due(task)
        job = take_due()
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            self.assertIsNotNone(Schedules(scope).binding(job["session_id"]))
            with patch(
                "services.retrieval.project_schedules.clock",
                return_value=clock() + timedelta(seconds=100),
            ):
                with self.assertRaises(ProjectError):
                    Schedules(scope).binding(job["session_id"])
                self.assertIsNone(Schedules(scope).claim(task["id"]))
        self.assertEqual(take_due(), {})

    def test_revoked_abandoned_claim_can_be_deleted_by_owner(self) -> None:
        task = self.create_task()
        self.due(task)
        self.assertTrue(take_due())
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "DELETE FROM atlas_projects.grants WHERE org_id=%s AND project_id=%s AND member_id=%s",
                (self.org, self.project, self.members["editor"]),
            )
            db.execute(
                "UPDATE atlas_projects.scheduled_tasks SET lease_until=now()-interval '1 minute' WHERE id=%s",
                (task["id"],),
            )
        self.assertEqual(take_due(), {})
        path = self.project_path() + "/scheduled-tasks/" + task["id"]
        paused = self.request("GET", path, "owner").json()
        self.assertFalse(paused["enabled"])
        self.assertEqual(paused["last_result"]["status"], "failed")
        response = self.request("DELETE", path, "owner", match=paused["revision"])
        self.assertEqual(response.status_code, 200, response.text)

    def test_proposal_cannot_run_before_confirmation(self) -> None:
        from services.retrieval.project_schedules import TaskInput

        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            value = Schedules(scope).create(
                TaskInput.model_validate(self.payload()), proposal=True
            )
        self.due(value)
        self.assertEqual(take_due(), {})
        path = self.project_path() + "/scheduled-tasks/" + value["id"] + "/confirm"
        self.assertEqual(self.request("POST", path, "owner", {}).status_code, 403)
        reply = self.request("POST", path, "editor", {})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertTrue(reply.json()["enabled"])
        self.assertFalse(reply.json()["proposal"])

    def test_due_work_with_stub_model_versions_same_file(self) -> None:
        self.assert_due_versions("Orchard.md")

    def test_due_work_with_stub_model_versions_document(self) -> None:
        self.assert_due_versions("Orchard.docx")

    def assert_due_versions(self, target: str) -> None:
        import asyncio
        from decimal import Decimal
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import AsyncMock
        from uuid import uuid4
        from psycopg.types.json import Jsonb
        from services.orchestrator.code_mode import CodeModel
        from services.orchestrator.model import Configuration
        from services.orchestrator.loop import Call, Turn, Reservation
        from services.orchestrator.project_scheduler import run_task
        from services.retrieval.api import app
        import test_project_auth as auth

        url = getattr(auth.ProjectAuthTests.serve, "__func__")(type(self), app)
        for name in (
            "stored_bytes",
            "temporary_bytes",
            "versioned_bytes",
            "upload_bytes",
            "derived_bytes",
        ):
            self.policy["quotas"][name] = 16777216
        self.policy["quotas"]["per_turn"]["expanded_bytes"] = 1048576
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.installation SET ceilings=%s",
                (Jsonb(self.policy),),
            )
            db.execute(
                "UPDATE atlas_projects.organizations SET policy=%s WHERE org_id=%s",
                (Jsonb(self.policy), self.org),
            )
        payload = dict(self.payload(), target_file=target)
        response = self.request(
            "POST", self.project_path() + "/scheduled-tasks", "editor", payload
        )
        self.assertEqual(response.status_code, 201, response.text)
        task = response.json()
        versions = []
        with (
            tempfile.TemporaryDirectory(dir="BRAIN") as directory,
            patch.dict(
                os.environ,
                dict(
                    ATLAS_PROJECT_MODE="1",
                    ATLAS_RETRIEVAL_URL=url,
                    ATLAS_PROJECT_IDENTITY_SECRET=os.urandom(32).hex(),
                    ATLAS_PROJECT_EXECUTION_SECRET=os.urandom(32).hex(),
                    ATLAS_DATA_DIR=directory,
                    ATLAS_CODE_STATE_DIR=str(Path(directory) / "state"),
                ),
            ),
        ):
            for ordinal in (1, 2):
                model = CodeModel(
                    "http://127.0.0.1:1",
                    Configuration(
                        input_eur_per_mtok=Decimal(0),
                        output_eur_per_mtok=Decimal(0),
                        max_tokens=2048,
                    ),
                )
                content = (
                    f"# Fictional orchard\nHarvest revision {ordinal}: 42 baskets.\n"
                )
                answers = [
                    Turn(
                        "",
                        (
                            Call(
                                str(uuid4()),
                                "publish_document"
                                if target.endswith(".docx")
                                else "publish_scheduled_file",
                                json.dumps(
                                    dict(path=target, content=content, sources=[])
                                    if target.endswith(".docx")
                                    else dict(content=content)
                                ),
                            ),
                        ),
                        Reservation(0, Decimal(0)),
                    ),
                    Turn(
                        "The fictional orchard report is ready.",
                        (),
                        Reservation(0, Decimal(0)),
                    ),
                ]
                self.due(task)
                job = take_due()
                self.assertTrue(job)
                with (
                    patch.object(CodeModel, "connect", AsyncMock(return_value=model)),
                    patch.object(CodeModel, "complete", AsyncMock(side_effect=answers)),
                    patch("services.orchestrator.session_updates.schedule"),
                ):
                    asyncio.run(run_task(job))
                result = self.request(
                    "GET",
                    self.project_path() + "/scheduled-tasks/" + task["id"],
                    "viewer",
                )
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(
                    result.json()["last_result"]["status"], "completed", result.text
                )
                files = self.request(
                    "GET", self.project_path() + "/files", "viewer"
                ).json()["items"]
                targets = [f for f in files if f["relative_path"] == target]
                self.assertEqual(len(targets), 1)
                versions.append(targets[0]["version_id"])
                file_id = targets[0]["id"]
                turns = self.request(
                    "GET",
                    self.project_path()
                    + "/conversations/"
                    + job["conversation_id"]
                    + "/turns",
                    "viewer",
                ).json()["items"]
                self.assertEqual(len(turns), 1)
                self.assertEqual(turns[0]["action"], "work")
                self.assertEqual(turns[0]["state"], "completed")
                self.assertIn("fictional orchard", turns[0]["content_refs"][-1]["text"])
            self.assertNotEqual(versions[0], versions[1])
            for ordinal, version in enumerate(versions, 1):
                response = self.request(
                    "GET",
                    self.project_path() + f"/files/{file_id}/content?version={version}",
                    "viewer",
                )
                self.assertEqual(response.status_code, 200, response.text)
                from services.retrieval.project_files import reopen_artifact

                text = (
                    reopen_artifact(response.content, ".docx").decode()
                    if target.endswith(".docx")
                    else response.text
                )
                self.assertIn(f"Harvest revision {ordinal}: 42 baskets.", text)

    def test_independent_database_connections_cannot_double_claim(self) -> None:
        from services.retrieval.project_store import ProjectPool, ScopedProjects

        task = self.create_task()
        self.due(task)
        repositories = [ScopedProjects(ProjectPool(self.app_dsn)) for _ in range(2)]
        for repository in repositories:
            self.addCleanup(repository.pool.close)

        def claim(repository: ScopedProjects) -> Any:
            with repository.scope(
                self.identity("editor"), self.org, self.project
            ) as scope:
                return Schedules(scope).claim(task["id"])

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, repositories))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_org_budget_blocks_claim_without_conversation(self) -> None:
        from psycopg.types.json import Jsonb

        task = self.create_task()
        self.due(task)
        self.policy["quotas"]["daily_model_eur"] = 0.01
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.installation SET ceilings=%s",
                (Jsonb(self.policy),),
            )
            db.execute(
                "UPDATE atlas_projects.organizations SET policy=%s WHERE org_id=%s",
                (Jsonb(self.policy), self.org),
            )
        # The normal Work reservation refuses before any provider call.
        job = take_due()
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            with self.assertRaises(ProjectError) as refused:
                from services.retrieval.project_work import Work

                session = Work(scope).get(job["session_id"])
                Work(scope).acquire(
                    dict(
                        session_id=session["id"],
                        request_id=job["run_id"],
                        expected_revision=session["revision"],
                        storage_bytes=0,
                    )
                )
            self.assertEqual(refused.exception.status, 429)

    def test_creator_daily_budget_refuses_before_work(self) -> None:
        import asyncio
        import os
        import tempfile
        from decimal import Decimal
        from unittest.mock import AsyncMock
        from services.orchestrator.daily_budget import DailyLedger
        from services.orchestrator.project_scheduler import run_task
        from services.retrieval.api import app
        import test_project_auth as auth

        url = getattr(auth.ProjectAuthTests.serve, "__func__")(type(self), app)
        task = self.create_task()
        self.due(task)
        job = take_due()
        with (
            tempfile.TemporaryDirectory(dir="BRAIN") as directory,
            patch.dict(
                os.environ,
                dict(
                    ATLAS_DATA_DIR=directory,
                    ATLAS_USER_DAILY_EUR="0.50",
                    ATLAS_RETRIEVAL_URL=url,
                    ATLAS_PROJECT_IDENTITY_SECRET=os.urandom(32).hex(),
                    ATLAS_PROJECT_EXECUTION_SECRET=os.urandom(32).hex(),
                ),
            ),
            patch(
                "services.orchestrator.project_scheduler.process_scoped_project",
                AsyncMock(),
            ) as work,
        ):
            ledger = DailyLedger()
            reserved = ledger.reserve_cost(job["subject"], Decimal("0.50"), "en")
            ledger.settle_cost(reserved, Decimal("0.49"))
            asyncio.run(run_task(job))
            work.assert_not_called()
        result = self.request(
            "GET", self.project_path() + "/scheduled-tasks/" + task["id"], "viewer"
        ).json()
        self.assertEqual(result["last_result"]["status"], "failed")
        self.assertEqual(result["failures"], 1)

    def test_conversation_tool_proposes_but_cannot_enable(self) -> None:
        import asyncio
        from decimal import Decimal
        import os
        from unittest.mock import AsyncMock
        from uuid import uuid4
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.model import GatewayModel, Configuration
        from services.orchestrator.loop import Call, Turn, Reservation
        from services.orchestrator.project_transcript import process_scoped_project
        from services.retrieval.api import app
        import test_project_auth as auth
        from psycopg.types.json import Jsonb

        self.policy["quotas"]["stored_bytes"] = 16777216
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.installation SET ceilings=%s",
                (Jsonb(self.policy),),
            )
            db.execute(
                "UPDATE atlas_projects.organizations SET policy=%s WHERE org_id=%s",
                (Jsonb(self.policy), self.org),
            )

        url = getattr(auth.ProjectAuthTests.serve, "__func__")(type(self), app)
        conversation = self.request(
            "POST",
            self.project_path() + "/conversations",
            "editor",
            dict(title="Fictional orchard report"),
        ).json()
        request = ChatRequest.model_validate(
            dict(
                org_id=self.org,
                project_id=self.project,
                conversation_id=conversation["id"],
                ui_locale="en",
                ui_timezone="Europe/Paris",
                messages=[
                    dict(
                        role="user",
                        content="Give a fictional orchard summary now and refresh it every three days at 09:00.",
                    )
                ],
            )
        )
        request._project_subject = "editor"
        request._transcript_key = str(uuid4())
        model = GatewayModel(
            "http://127.0.0.1:1",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=2048,
            ),
        )
        answers = [
            Turn(
                "",
                (
                    Call(
                        str(uuid4()),
                        "propose_scheduled_task",
                        json.dumps(self.payload()),
                    ),
                ),
                Reservation(0, Decimal(0)),
            ),
            Turn(
                "Fictional orchard: 42 baskets harvested. Confirm the proposed schedule to refresh it every three days.",
                (),
                Reservation(0, Decimal(0)),
            ),
        ]
        item = Interaction()
        with (
            patch.dict(
                os.environ,
                dict(
                    ATLAS_PROJECT_MODE="1",
                    ATLAS_RETRIEVAL_URL=url,
                    ATLAS_PROJECT_IDENTITY_SECRET=os.urandom(32).hex(),
                    ATLAS_PROJECT_EXECUTION_SECRET=os.urandom(32).hex(),
                ),
            ),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(GatewayModel, "complete", AsyncMock(side_effect=answers)),
            patch("services.orchestrator.session_updates.schedule"),
        ):
            asyncio.run(process_scoped_project(request, item))
        self.assertEqual(item.state, "done", item.erreurs)
        self.assertIn("42 baskets", item.reponse)
        tasks = self.request(
            "GET", self.project_path() + "/scheduled-tasks", "editor"
        ).json()
        self.assertTrue(tasks["can_manage"])
        self.assertEqual(len(tasks["items"]), 1)
        proposal = tasks["items"][0]
        self.assertTrue(proposal["proposal"])
        self.assertFalse(proposal["enabled"])
        self.assertEqual(proposal["schedule"]["days"], 3)
        self.assertEqual(proposal["schedule"]["timezone"], "Europe/Paris")
        self.assertEqual(proposal["conversation_id"], conversation["id"])
        self.due(proposal)
        self.assertEqual(take_due(), {})
        self.assertFalse(
            self.request(
                "GET", self.project_path() + "/scheduled-tasks", "viewer"
            ).json()["can_manage"]
        )
