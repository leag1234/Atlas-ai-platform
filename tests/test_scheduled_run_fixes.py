"""Scheduled and Work run fixes of 2026-11-16: date, spend, logs, readiness, identity.

Fictional names and data only. No provider, network or database is used: the
retrieval side of the scheduler is covered by test_project_schedules.
"""

import asyncio
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from unittest import mock

import aiohttp

from services.orchestrator import budget_identity, project_scheduler, work_clock
from services.orchestrator import work_opencode_turn as turn_module
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.code_mode import code_profile
from services.orchestrator.daily_budget import DailyLedger
from services.orchestrator.interactions import Interaction
from services.orchestrator.key_subject import SubjectError
from services.orchestrator.project_transcript import failure_message
from services.retrieval.project_identity import ProjectError

LATE = datetime(2026, 11, 16, 23, 30, tzinfo=timezone.utc)


def work_request(**changes: Any) -> ChatRequest:
    return ChatRequest.model_validate(
        dict(
            model=code_profile(),
            messages=[dict(role="user", content="Write today's date in a note.")],
            **changes,
        )
    )


class WorkClockTests(unittest.TestCase):
    def test_today_follows_the_person_zone_across_midnight_utc(self) -> None:
        paris = work_clock.current_moment("Europe/Paris", LATE)
        self.assertEqual(
            paris,
            dict(
                date="2026-11-17",
                weekday="Tuesday",
                time="00:30",
                time_zone="Europe/Paris",
                utc_offset="+01:00",
            ),
        )
        self.assertEqual(work_clock.current_moment("UTC", LATE)["date"], "2026-11-16")
        self.assertEqual(
            work_clock.current_moment("America/New_York", LATE)["date"], "2026-11-16"
        )

    def test_zone_of_the_request(self) -> None:
        # No browser zone (the request default): the chat's home zone, as the
        # date tool does. A scheduled task's own zone is used as chosen.
        self.assertEqual(work_clock.request_zone(work_request()), "Europe/Paris")
        self.assertEqual(
            work_clock.request_zone(work_request(ui_timezone="Asia/Tokyo")),
            "Asia/Tokyo",
        )
        scheduled = work_request(ui_timezone="UTC")
        scheduled._scheduled_run = True
        self.assertEqual(work_clock.request_zone(scheduled), "UTC")
        self.assertEqual(work_clock.person_zone("Not/AZone"), "Europe/Paris")

    def test_every_work_turn_gets_today_in_its_context(self) -> None:
        context = work_clock.work_context(
            work_request(ui_timezone="Europe/Paris"), LATE
        )
        self.assertEqual(context["now"]["date"], "2026-11-17")
        self.assertNotIn("scheduled_deliverable", context)

    def test_scheduled_deliverable_note_says_whether_a_version_exists(self) -> None:
        request = work_request(ui_timezone="Europe/Paris")
        request._scheduled_run = True
        request._scheduled_target = "Daily-note.md"
        request.__dict__["scheduled_target_exists"] = False
        note = work_clock.work_context(request, LATE)["scheduled_deliverable"]
        self.assertIn("Daily-note.md with publish_scheduled_file", note)
        self.assertIn("no previous version", note)
        request.__dict__["scheduled_target_exists"] = True
        note = work_clock.work_context(request, LATE)["scheduled_deliverable"]
        self.assertIn("keeps the earlier ones", note)
        self.assertIn(
            "publish_document",
            str(work_clock.scheduled_deliverable("Orchard.docx", False)),
        )
        self.assertIsNone(work_clock.scheduled_deliverable(None, None))


class EnginePreambleTests(unittest.IsolatedAsyncioTestCase):
    async def test_engine_turn_starts_with_today(self) -> None:
        class Engine:
            url, password, name = "http://engine.invalid", "unused", "engine-test"

        class Tools:
            deliverables: list[Any] = []

            async def emit(self, kind: str, **data: Any) -> None:
                return None

        class Limits:
            cost, wall_clock = Decimal("1"), 600.0

        driver = mock.Mock()
        driver.run = mock.AsyncMock(return_value={"outcome": "complete", "cost": 0})
        request = work_request(ui_timezone="Europe/Paris")
        with (
            mock.patch.object(
                turn_module, "start", mock.AsyncMock(return_value=Engine())
            ),
            mock.patch.object(turn_module, "stop", mock.AsyncMock()),
            mock.patch.object(turn_module, "emit_changes", mock.AsyncMock()),
            mock.patch.object(turn_module, "OpencodeDriver", return_value=driver),
            mock.patch("services.orchestrator.datecalc.utc_now", return_value=LATE),
        ):
            await turn_module.run_opencode_turn(
                request,
                Interaction(),
                cast(Any, Tools()),
                mock.Mock(),
                cast(Any, Limits()),
                False,
            )
        prompt = driver.run.await_args.args[0]
        self.assertTrue(prompt.startswith("Current date and time: Tuesday 2026-11-17"))
        self.assertTrue(prompt.endswith("Write today's date in a note."))


class ScheduledPublishToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_refused_publication_is_a_tool_result_not_a_crash(self) -> None:
        # Staging 2026-11-16: a refusal raised out of the tool ended the whole
        # run as failed after the file had been saved.
        from services.orchestrator.code_tools import WorkTools
        from services.orchestrator.loop import Call

        events: list[dict[str, Any]] = []

        async def call(operation: str, data: Any, key: Any = None) -> Any:
            if operation == "scheduled-publish":
                raise ProjectError(422, "invalid_request", "Use publish_document")
            events.extend(data["progress"])
            return {"events": []}

        execution = mock.Mock()
        execution.check = mock.AsyncMock()
        execution.fence = mock.Mock(return_value={})
        execution.broker.call = call
        execution.session = {"id": "fictional-session"}
        tools = WorkTools(mock.Mock(), execution)
        result = await tools.execute(
            Call("c1", "publish_scheduled_file", json.dumps({"content": "Note"})), 10
        )
        self.assertEqual(result["error"], "invalid_request")
        self.assertEqual(events[-1]["status"], "failed")
        self.assertEqual(
            tools.validation_failures["publish_scheduled_file"], "invalid_request"
        )


class FailureMessageTests(unittest.TestCase):
    def test_known_failure_stores_its_public_reply_in_every_language(self) -> None:
        # Staging 2026-11-16: the code "cost" became a stray visible answer.
        item = Interaction(state="failed", erreurs=["cost"])
        item.work_failure = {"category": "budget"}
        item.reponse = "The work stopped at its spending limit."
        self.assertEqual(failure_message(item, "en"), item.reponse)
        self.assertEqual(failure_message(item, "de"), item.reponse)

    def test_unknown_failure_keeps_its_diagnostic_codes(self) -> None:
        item = Interaction(state="failed", erreurs=["provider_error"])
        item.reponse = "Explicit stop: provider_error."
        self.assertEqual(failure_message(item, "en"), "provider_error")
        self.assertEqual(failure_message(item, "fr"), item.reponse)


def job(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = dict(
        task=dict(
            id="0b9b8f6e-2a7c-4f43-9e55-6f0f4f2f9a10",
            org_id="5f0d8a3e-1d2c-4b5a-8e7f-9a0b1c2d3e4f",
            project_id="6a1e9b4f-2e3d-4c6b-9f80-ab1c2d3e4f50",
            name="Fictional orchard note",
            instruction="Write today's orchard note for the fictional Lumen farm.",
            schedule=dict(frequency="daily", time="09:00", timezone="Europe/Paris"),
            target_file="Orchard-note.md",
            locale="en",
        ),
        run_id="7c2f0e5d-3f4e-4d7c-8a91-bc2d3e4f5061",
        conversation_id="8d3a1f6e-4a5f-4e8d-9ba2-cd3e4f506172",
        session_id="9e4b2a7f-5b6a-4f9e-8cb3-de4f50617283",
        reserved_eur=0.5,
        day="2026-11-16",
        target_exists=False,
        subject="ada.lumen@example.org",
    )
    value.update(changes)
    return value


class RunTaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        environment = mock.patch.dict(
            os.environ,
            {
                "ATLAS_DATA_DIR": directory.name,
                "ATLAS_USER_DAILY_EUR": "5",
                "ATLAS_PROJECT_EXECUTION_SECRET": "x" * 32,
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        owners = mock.patch("services.orchestrator.request_owners.record")
        owners.start()
        self.addCleanup(owners.stop)
        bucket = mock.patch.object(
            budget_identity,
            "person_bucket",
            mock.AsyncMock(return_value="acct-ada-lumen"),
        )
        bucket.start()
        self.addCleanup(bucket.stop)

    def measured(self, user: str) -> int:
        with sqlite3.connect(os.path.join(self.directory, "daily-budget.sqlite")) as db:
            row = db.execute(
                "SELECT reserved_micro, measured_micro FROM budget_ledger "
                "WHERE kind='day' AND user=?",
                (user,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 0, "no reservation may stay open")
        return int(row[1])

    async def run_job(
        self, finished: dict[str, Any], outcome: Any
    ) -> tuple[list[ChatRequest], list[dict[str, Any]], list[str]]:
        seen: list[ChatRequest] = []
        calls: list[dict[str, Any]] = []

        async def work(request: ChatRequest, item: Interaction) -> None:
            seen.append(request)
            await outcome(item)

        async def call(operation: str, data: Any, key: Any = None) -> Any:
            calls.append(data)
            return finished

        with (
            mock.patch.object(project_scheduler, "process_scoped_project", work),
            mock.patch.object(
                project_scheduler.ScheduleBroker, "call", side_effect=call
            ),
            self.assertLogs(project_scheduler.__name__, logging.WARNING) as logs,
        ):
            await project_scheduler.run_task(job())
        return seen, calls, logs.output

    async def test_failed_run_charges_the_measured_cost_and_logs_metadata(
        self,
    ) -> None:
        async def failed(item: Interaction) -> None:
            item.state, item.cout_eur = "failed", 0.013

        finished = dict(
            last_result=dict(status="failed", reason="work_failed", cost_eur=0.013)
        )
        seen, calls, logs = await self.run_job(finished, failed)
        self.assertEqual(calls[-1]["status"], "failed")
        self.assertEqual(calls[-1]["cost_eur"], 0.013)
        # The creator's one bucket is charged what the run cost, not 0.50.
        self.assertEqual(self.measured("acct-ada-lumen"), 13000)
        request = seen[0]
        self.assertEqual(request.messages[-1].text, job()["task"]["instruction"])
        self.assertEqual(request.ui_timezone, "Europe/Paris")
        self.assertIs(request.__dict__["scheduled_target_exists"], False)
        self.assertEqual(request.__dict__["daily_user"], "acct-ada-lumen")
        tag = project_scheduler.task_tag(job()["task"]["id"])
        self.assertTrue(
            any("scheduled_run_started task=" + tag in line for line in logs)
        )
        finished_lines = [line for line in logs if "scheduled_run_finished" in line]
        self.assertEqual(len(finished_lines), 1)
        self.assertIn("status=failed", finished_lines[0])
        self.assertIn("cost_eur=0.013", finished_lines[0])
        self.assertIn("reason=work_failed", finished_lines[0])
        text = "\n".join(logs)
        for private in ("orchard", "Lumen", "Orchard-note", job()["task"]["id"]):
            self.assertNotIn(private, text)

    async def test_completed_run_logs_its_success(self) -> None:
        async def completed(item: Interaction) -> None:
            item.state, item.cout_eur = "done", 0.02

        finished = dict(last_result=dict(status="completed", cost_eur=0.02))
        _, calls, logs = await self.run_job(finished, completed)
        self.assertEqual(calls[-1]["status"], "completed")
        self.assertEqual(self.measured("acct-ada-lumen"), 20000)
        self.assertTrue(
            any(
                "scheduled_run_finished" in line and "status=completed" in line
                for line in logs
            )
        )

    async def test_unknown_cost_keeps_the_reservation(self) -> None:
        # The finish call fails: what the run cost is not known.
        async def failed(request: ChatRequest, item: Interaction) -> None:
            item.state = "failed"

        async def broken(*_: Any, **__: Any) -> Any:
            raise ProjectError(503, "dependency_unavailable")

        with (
            mock.patch.object(project_scheduler, "process_scoped_project", failed),
            mock.patch.object(
                project_scheduler.ScheduleBroker, "call", side_effect=broken
            ),
            self.assertLogs(project_scheduler.__name__, logging.WARNING),
        ):
            await project_scheduler.run_task(job())
        self.assertEqual(self.measured("acct-ada-lumen"), 500000)


class SchedulerReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def run_loop(
        self, failures: list[BaseException], clock: list[float]
    ) -> tuple[list[float], list[str]]:
        sleeps: list[float] = []
        outcomes = list(failures)

        async def tick() -> dict[str, Any]:
            if outcomes:
                raise outcomes.pop(0)
            return {}

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if seconds == 60:
                raise asyncio.CancelledError

        times = iter(clock)
        with (
            mock.patch.object(project_scheduler, "tick", tick),
            mock.patch.object(project_scheduler, "pause", sleep),
            mock.patch.object(project_scheduler, "monotonic", lambda: next(times)),
            self.assertLogs(project_scheduler.__name__, logging.DEBUG) as logs,
        ):
            logging.getLogger(project_scheduler.__name__).debug("start")
            with self.assertRaises(asyncio.CancelledError):
                await project_scheduler.loop()
        return sleeps, logs.output

    @staticmethod
    def refused() -> aiohttp.ClientConnectorError:
        return aiohttp.ClientConnectorError(
            mock.Mock(), OSError(111, "Connection refused")
        )

    async def test_first_ticks_wait_for_retrieval_without_an_error(self) -> None:
        sleeps, logs = await self.run_loop(
            [self.refused(), ProjectError(503, "dependency_unavailable")],
            [0.0, 1.0, 6.0, 11.0],
        )
        self.assertEqual(sleeps, [5.0, 5.0, 60])
        self.assertFalse([line for line in logs if "scheduler_tick_failed" in line])

    async def test_retrieval_down_after_the_grace_is_reported(self) -> None:
        sleeps, logs = await self.run_loop([self.refused()], [0.0, 500.0])
        self.assertEqual(sleeps, [60])
        self.assertTrue([line for line in logs if "scheduler_tick_failed" in line])


class BudgetIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        budget_identity._cache.clear()
        self.addCleanup(budget_identity._cache.clear)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = mock.patch.dict(os.environ, {"ATLAS_DATA_DIR": directory.name})
        environment.start()
        self.addCleanup(environment.stop)

    async def test_email_seen_with_the_account_id_joins_its_bucket(self) -> None:
        # The interface forwards the account id and email together on a chat:
        # the same person's project and scheduled spend then joins that bucket,
        # with no user directory configured (the deployed case).
        ledger = DailyLedger()
        ledger.remember_alias("Ada.Lumen@example.org", "acct-ada-lumen")
        with mock.patch.object(
            budget_identity,
            "resolve_login",
            mock.AsyncMock(
                side_effect=SubjectError("user_directory_unconfigured", 503)
            ),
        ) as resolve:
            self.assertEqual(
                await budget_identity.person_bucket("ada.lumen@example.org", ledger),
                "acct-ada-lumen",
            )
        resolve.assert_not_awaited()
        # Never an email, an own-budget key or an empty id as the bucket.
        for bucket in ("bo.quill@example.org", "api-key:k1", " "):
            ledger.remember_alias("bo.quill@example.org", bucket)
        self.assertIsNone(ledger.alias("bo.quill@example.org"))
        ledger.remember_alias("ada.lumen@example.org", "acct-ada-renamed")
        self.assertEqual(ledger.alias("ADA.LUMEN@example.org"), "acct-ada-renamed")

    async def test_project_subject_shares_the_account_bucket(self) -> None:
        # Staging 2026-11-16: one person had a bucket for their account id
        # (chat, API keys) and another for their email (projects, schedules).
        with mock.patch.object(
            budget_identity,
            "resolve_login",
            mock.AsyncMock(return_value=("Ada.Lumen@example.org", "acct-ada-lumen")),
        ) as resolve:
            self.assertEqual(
                await budget_identity.person_bucket("ada.lumen@example.org"),
                "acct-ada-lumen",
            )
            self.assertEqual(
                await budget_identity.person_bucket("Ada.Lumen@example.org"),
                "acct-ada-lumen",
            )
        resolve.assert_awaited_once()

    async def test_unresolved_subject_keeps_its_email_bucket(self) -> None:
        with mock.patch.object(
            budget_identity,
            "resolve_login",
            mock.AsyncMock(side_effect=SubjectError("user_never_signed_in", 404)),
        ):
            self.assertEqual(
                await budget_identity.person_bucket("bo.quill@example.org"),
                "bo.quill@example.org",
            )
        self.assertEqual(await budget_identity.person_bucket("acct-7"), "acct-7")

    def test_exemption_by_email_holds_for_the_account_bucket(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(
                os.environ,
                {
                    "ATLAS_USER_DAILY_EUR": "0.10",
                    "ATLAS_BUDGET_EXEMPT_USERS": "ada.lumen@example.org",
                },
            ),
        ):
            ledger = DailyLedger(path=Path(directory) / "ledger.sqlite")
            reserved = ledger.reserve_cost(
                "acct-ada-lumen",
                Decimal("0.50"),
                "en",
                aliases=("ada.lumen@example.org",),
            )
            ledger.settle_cost(reserved, Decimal("0.50"))
            from services.orchestrator.daily_budget import BudgetRefusal

            with self.assertRaises(BudgetRefusal):
                ledger.reserve_cost("acct-bo-quill", Decimal("0.50"), "en")


if __name__ == "__main__":
    unittest.main()
