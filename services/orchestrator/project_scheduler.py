"""Minute scheduler in the service process; database claims coordinate replicas."""

import asyncio
import hashlib
from contextlib import asynccontextmanager, suppress
from decimal import Decimal
import logging
import os
import time
from typing import Any, AsyncIterator
from uuid import uuid4

import aiohttp
from fastapi import FastAPI

from services.orchestrator.code_api import ExecutionBroker
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.daily_budget import DailyLedger, BudgetRefusal
from services.orchestrator.project_transcript import process_scoped_project
from services.retrieval.project_identity import ProjectError


class ScheduleBroker(ExecutionBroker):
    private_prefix = "/internal/atlas/scheduler/"


async def tick() -> dict[str, Any]:
    from services.orchestrator.chat_pipeline import retrieval_url
    from services.orchestrator.project_ui import bounded_json

    async with aiohttp.ClientSession(
        trust_env=False, timeout=aiohttp.ClientTimeout(total=20)
    ) as client:
        async with client.post(
            retrieval_url() + "/internal/atlas/scheduler/tick",
            headers={
                "X-Atlas-Execution-Secret": os.environ.get(
                    "ATLAS_PROJECT_EXECUTION_SECRET", ""
                ),
                "X-Atlas-Execution-Audience": "atlas-retrieval-execution",
                "Idempotency-Key": str(uuid4()),
            },
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise ProjectError(response.status, "dependency_unavailable")
            return await bounded_json(response)


STARTUP_GRACE_SECONDS = 180.0
NOT_READY_RETRY_SECONDS = 5.0


def task_tag(identifier: str) -> str:
    """A task's id in logs: a short hash, never the id, name or content."""
    return hashlib.sha256(identifier.encode()).hexdigest()[:12]


async def run_task(job: dict[str, Any]) -> None:
    from services.orchestrator.budget_identity import person_bucket
    from services.orchestrator.code_mode import code_profile

    task = job["task"]
    tag, started = task_tag(str(task["id"])), time.monotonic()
    log = logging.getLogger(__name__)
    # Metadata only (staging 2026-11-16: a successful run left no line at all).
    log.warning(
        "scheduled_run_started task=%s reserved_eur=%s", tag, job["reserved_eur"]
    )
    broker = ScheduleBroker(job["subject"], task["org_id"], task["project_id"])
    ledger, reservation = DailyLedger(), None
    item = Interaction(request_id="chatcmpl-" + job["run_id"])
    from services.orchestrator.request_owners import record as record_owner

    record_owner(item.request_id, job["subject"])
    owner = asyncio.current_task()
    assert owner is not None
    reason = ""

    async def heartbeat() -> None:
        try:
            while True:
                await asyncio.sleep(5)
                await broker.call(
                    "finish",
                    dict(task_id=task["id"], run_id=job["run_id"], status="renew"),
                )
        except (ProjectError, aiohttp.ClientError, TimeoutError):
            owner.cancel()

    renewal = asyncio.create_task(heartbeat())
    try:
        if os.environ.get("ATLAS_KILL_SWITCH_FILE"):
            from pathlib import Path

            if Path(os.environ["ATLAS_KILL_SWITCH_FILE"]).exists():
                raise BudgetRefusal("maintenance", 0, 0, task["locale"])
        # The creator's one daily bucket, as for their chat (budget_identity.py).
        bucket = await person_bucket(job["subject"], ledger)
        reservation = ledger.reserve_cost(
            bucket,
            Decimal(str(job["reserved_eur"])),
            task["locale"],
            aliases=(job["subject"],),
        )
        # The instruction alone is the user's message: the deliverable note is
        # model context (work_clock.py), never shown as the user's words.
        request = ChatRequest.model_validate(
            dict(
                model=code_profile(),
                org_id=task["org_id"],
                project_id=task["project_id"],
                conversation_id=job["conversation_id"],
                work_session_id=job["session_id"],
                project_action="work",
                ui_locale=task["locale"],
                # The creator's zone: "today" in the run is their today.
                ui_timezone=task["schedule"]["timezone"],
                messages=[dict(role="user", content=task["instruction"])],
                stream=False,
            )
        )
        request._scheduled_run = True
        request._scheduled_target = task["target_file"]
        request._project_subject = job["subject"]
        request._transcript_key = "scheduled:" + job["run_id"]
        request.__dict__.update(
            daily_ledger=ledger,
            daily_user=bucket,
            scheduled_target_exists=bool(job.get("target_exists")),
        )
        await process_scoped_project(request, item)
    except (
        ProjectError,
        BudgetRefusal,
        aiohttp.ClientError,
        TimeoutError,
        ValueError,
    ) as exc:
        reason = (
            str(exc.body.get("error"))
            if isinstance(exc, ProjectError)
            else exc.category
            if isinstance(exc, BudgetRefusal)
            else "transport"
        )
        log.warning(
            "scheduled_work_failed kind=%s status=%s code=%s",
            type(exc).__name__,
            getattr(exc, "status", 0),
            exc.body.get("error")
            if isinstance(exc, ProjectError)
            else "budget_or_transport",
        )
    finally:
        renewal.cancel()
        with suppress(asyncio.CancelledError):
            await renewal
        status = "completed" if item.state == "done" else "failed"
        charged: Any = None
        try:
            finished = await broker.call(
                "finish",
                dict(
                    task_id=task["id"],
                    run_id=job["run_id"],
                    status=status,
                    cost_eur=item.cout_eur,
                ),
            )
            last = finished.get("last_result") or {}
            status = str(last.get("status") or status)
            reason = str(last.get("reason") or reason)
            charged = last.get("cost_eur")
        except ProjectError as exc:
            log.warning("scheduled_finish_failed status=%s", exc.status)
        finally:
            if reservation is not None:
                # The cost the task was charged: measured, or the reservation
                # only when the cost could not be known (project_schedules.py).
                ledger.settle_cost(
                    reservation,
                    Decimal(str(charged))
                    if charged is not None
                    else Decimal(str(item.cout_eur))
                    if item.state == "done"
                    else Decimal(str(job["reserved_eur"])),
                )
            log.warning(
                "scheduled_run_finished task=%s status=%s cost_eur=%s duration_s=%.1f reason=%s",
                tag,
                status,
                charged if charged is not None else item.cout_eur,
                time.monotonic() - started,
                reason or "-",
            )


def monotonic() -> float:
    return time.monotonic()


async def pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


def not_ready(exc: BaseException) -> bool:
    """Retrieval not listening yet, or answering that its database is not up."""
    return isinstance(exc, aiohttp.ClientConnectorError) or (
        isinstance(exc, ProjectError) and exc.status in {502, 503, 504}
    )


async def loop() -> None:
    active: set[asyncio.Task[None]] = set()
    ready, began = False, monotonic()

    def finished(task: asyncio.Task[None]) -> None:
        active.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logging.getLogger(__name__).warning(
                "scheduled_worker_failed kind=%s", type(task.exception()).__name__
            )

    try:
        while True:
            try:
                while len(active) < 4 and (job := await tick()):
                    worker = asyncio.create_task(run_task(job))
                    active.add(worker)
                    worker.add_done_callback(finished)
                ready = True
            except (
                ProjectError,
                aiohttp.ClientError,
                TimeoutError,
                OSError,
                ValueError,
            ) as exc:
                # Both services start together: the first tick after a restart
                # found retrieval not listening yet and logged a failure
                # (staging 2026-11-16). Wait for it instead, within a bound.
                if (
                    not ready
                    and not_ready(exc)
                    and monotonic() - began < STARTUP_GRACE_SECONDS
                ):
                    await pause(NOT_READY_RETRY_SECONDS)
                    continue
                logging.getLogger(__name__).warning(
                    "scheduler_tick_failed kind=%s", type(exc).__name__
                )
            await pause(60)
    finally:
        remaining = list(active)
        for worker in remaining:
            worker.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from services.orchestrator.work_orphan_sweep import sweep_at_startup

    # A restart during a Work turn can leave engine, proxy or worker
    # containers running with no session behind them: sweep them once at start.
    if os.environ.get("ATLAS_PROJECT_MODE") == "1":
        try:
            await sweep_at_startup()
        except Exception:
            logging.getLogger(__name__).warning(
                "work_orphan_sweep_startup_failed", exc_info=True
            )
    worker = (
        asyncio.create_task(loop())
        if os.environ.get("ATLAS_PROJECT_MODE") == "1"
        and os.environ.get("ATLAS_SCHEDULED_TASKS") == "1"
        else None
    )
    try:
        yield
    finally:
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
