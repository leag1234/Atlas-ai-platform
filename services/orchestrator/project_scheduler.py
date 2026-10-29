"""Minute scheduler in the service process; database claims coordinate replicas."""

import asyncio
from contextlib import asynccontextmanager, suppress
from decimal import Decimal
import logging
import os
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


async def run_task(job: dict[str, Any]) -> None:
    from services.orchestrator.code_mode import code_profile

    task = job["task"]
    broker = ScheduleBroker(job["subject"], task["org_id"], task["project_id"])
    ledger, reservation = DailyLedger(), None
    item = Interaction(request_id="chatcmpl-" + job["run_id"])
    owner = asyncio.current_task()
    assert owner is not None

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
        reservation = ledger.reserve_cost(
            job["subject"], Decimal(str(job["reserved_eur"])), task["locale"]
        )
        instruction = task["instruction"]
        if task["target_file"]:
            instruction += (
                "\n\nScheduled deliverable: publish the result to "
                + task["target_file"]
                + ". Update this file; retain its previous version."
            )
        request = ChatRequest.model_validate(
            dict(
                model=code_profile(),
                org_id=task["org_id"],
                project_id=task["project_id"],
                conversation_id=job["conversation_id"],
                work_session_id=job["session_id"],
                project_action="work",
                ui_locale=task["locale"],
                messages=[dict(role="user", content=instruction)],
                stream=False,
            )
        )
        request._scheduled_run = True
        request._scheduled_target = task["target_file"]
        request._project_subject = job["subject"]
        request._transcript_key = "scheduled:" + job["run_id"]
        request.__dict__.update(daily_ledger=ledger, daily_user=job["subject"])
        await process_scoped_project(request, item)
    except (
        ProjectError,
        BudgetRefusal,
        aiohttp.ClientError,
        TimeoutError,
        ValueError,
    ) as exc:
        logging.getLogger(__name__).warning(
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
        if reservation is not None:
            ledger.settle_cost(
                reservation,
                Decimal(str(item.cout_eur))
                if item.state == "done"
                else Decimal(str(job["reserved_eur"])),
            )
        try:
            await broker.call(
                "finish",
                dict(
                    task_id=task["id"],
                    run_id=job["run_id"],
                    status="completed" if item.state == "done" else "failed",
                    cost_eur=item.cout_eur,
                ),
            )
        except ProjectError as exc:
            logging.getLogger(__name__).warning(
                "scheduled_finish_failed status=%s", exc.status
            )


async def loop() -> None:
    active: set[asyncio.Task[None]] = set()

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
            except (
                ProjectError,
                aiohttp.ClientError,
                TimeoutError,
                OSError,
                ValueError,
            ) as exc:
                logging.getLogger(__name__).warning(
                    "scheduler_tick_failed kind=%s", type(exc).__name__
                )
            await asyncio.sleep(60)
    finally:
        remaining = list(active)
        for worker in remaining:
            worker.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
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
