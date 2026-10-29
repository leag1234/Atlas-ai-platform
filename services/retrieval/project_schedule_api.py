"""Private scheduler transport. Only the execution broker may discover due work."""

import asyncio
import os
import time
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
import psycopg

from services.retrieval.project_identity import (
    Identity,
    ProjectError,
    authenticate_broker,
    verify,
)
from services.retrieval.project_scoped_api import store
from services.retrieval.project_schedules import Schedules

router = APIRouter()


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: UUID
    run_id: UUID
    status: Literal["renew", "completed", "failed"]
    cost_eur: float = Field(default=0, ge=0, allow_inf_nan=False)


def take_due() -> dict[str, Any]:
    repository = store(os.environ.get("ATLAS_PROJECT_DSN", ""))
    with repository.pool.connection() as db:
        db.execute("SELECT atlas_projects.recover_expired_work_leases(NULL)")
    with repository.pool.connection() as db:
        candidates = db.execute(
            "SELECT * FROM atlas_projects.scheduled_candidates()"
        ).fetchall()
    for candidate in candidates:
        identity = Identity(
            candidate["subject"],
            str(candidate["member_id"]),
            str(candidate["org_id"]),
            str(candidate["project_id"]),
            candidate["membership_revision"],
            candidate["policy_revision"],
            int(time.time()) + 60,
        )
        try:
            with repository.scope(identity, identity.org, identity.project) as scope:
                claimed = Schedules(scope).claim(str(candidate["id"]))
                if claimed:
                    return dict(claimed, subject=identity.subject)
        except ProjectError as exc:
            if exc.status not in {403, 404, 409, 429}:
                raise
            if exc.status == 429:
                with repository.scope(
                    identity, identity.org, identity.project
                ) as scope:
                    schedules = Schedules(scope)
                    row = schedules.row(str(candidate["id"]))
                    from packages.task_schedule import TaskSchedule, next_run
                    from services.retrieval.project_schedules import clock

                    row["next_run"] = next_run(
                        TaskSchedule.model_validate(row["data"]["schedule"]), clock()
                    )
                    schedules.failed(row, "budget_cap")
    return {}


def complete(identity: Identity, data: Completion) -> dict[str, Any]:
    with store(os.environ.get("ATLAS_PROJECT_DSN", "")).scope(
        identity, identity.org, identity.project
    ) as scope:
        scope.authorize("POST", ["scheduled-tasks"])
        schedules = Schedules(scope)
        row = schedules.row(str(data.task_id))
        if str(row["owner_member_id"]) != identity.member:
            raise ProjectError(403, "permission_denied")
        return schedules.finish(
            str(data.task_id), str(data.run_id), data.status, data.cost_eur
        )


@router.post("/internal/atlas/scheduler/{operation}")
async def scheduler(request: Request, operation: str) -> Response:
    headers = {"Cache-Control": "private, no-store"}
    try:
        if not request.headers.get("x-atlas-execution-secret"):
            credential = request.headers.get("x-atlas-project-identity")
            if credential:
                verify(
                    credential, os.environ.get("ATLAS_PROJECT_SIGNING_KEY", "").encode()
                )
                raise ProjectError(404, "not_found")
            raise ProjectError(401, "unauthenticated")
        authenticate_broker(
            request.headers.get("x-atlas-execution-secret", ""),
            os.environ.get("ATLAS_PROJECT_EXECUTION_SECRET", ""),
        )
        if (
            request.headers.get("x-atlas-execution-audience")
            != "atlas-retrieval-execution"
        ):
            raise ProjectError(401, "unauthenticated")
        if "origin" in request.headers or any(
            k.startswith("sec-fetch-") for k in request.headers
        ):
            raise ProjectError(403, "origin_rejected")
        if (
            not request.headers.get("idempotency-key")
            or len(request.headers["idempotency-key"]) > 200
        ):
            raise ProjectError(422, "invalid_request")
        if operation == "tick":
            result = await asyncio.to_thread(take_due)
        elif operation == "finish":
            identity = verify(
                request.headers.get("x-atlas-project-identity", ""),
                os.environ.get("ATLAS_PROJECT_SIGNING_KEY", "").encode(),
            )
            raw = bytearray()
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > 4096:
                        raise ProjectError(413, "size_limit")
            data = Completion.model_validate_json(bytes(raw))
            result = await asyncio.to_thread(complete, identity, data)
        else:
            raise ProjectError(404, "not_found")
        return JSONResponse(result, headers=headers)
    except ProjectError as exc:
        return JSONResponse(exc.body, status_code=exc.status, headers=headers)
    except ValueError:
        return JSONResponse(
            ProjectError(422, "invalid_request").body, status_code=422, headers=headers
        )
    except (psycopg.Error, OSError, TimeoutError):
        return JSONResponse(
            ProjectError(503, "dependency_unavailable").body,
            status_code=503,
            headers=headers,
        )
