"""Internal personal-memory API; identity comes only from the trusted chat adapter."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from packages.user_memory import MemorySetting, MemoryWrite
from packages.user_memory_auth import service_secret
from services.retrieval.project_identity import ProjectError, authenticate_broker
from services.retrieval.project_store import ProjectPool
from services.retrieval.user_memory import UserMemory

router = APIRouter()


@lru_cache(maxsize=8)
def store(path: str, dsn: str) -> UserMemory:
    return UserMemory(pool=ProjectPool(dsn)) if dsn else UserMemory(Path(path))


def organization(memory: UserMemory) -> str:
    configured = os.environ.get("ATLAS_USER_MEMORY_ORG")
    if configured:
        return configured
    if memory.pool:
        with memory.pool.connection() as db:
            row = db.execute(
                "SELECT initial_org FROM atlas_projects.installation"
            ).fetchone()
            if row:
                return str(row["initial_org"])
        raise ProjectError(503, "dependency_unavailable")
    return "stable"


@router.api_route(
    "/internal/atlas/user-memory", methods=["GET", "POST", "PATCH", "DELETE"]
)
@router.api_route("/internal/atlas/user-memory/{path}", methods=["PATCH", "DELETE"])
async def memory_api(request: Request, path: str = "") -> JSONResponse:
    try:
        authenticate_broker(
            request.headers.get("x-atlas-memory-secret", ""), service_secret()
        )
        subject = request.headers.get("x-atlas-memory-subject", "")
        if not subject or len(subject) > 200 or subject == "unknown":
            raise ProjectError(401, "unauthenticated")
        memory = store(
            os.environ.get("ATLAS_USER_MEMORY_DB", "BRAIN/user-memory.sqlite"),
            os.environ.get("ATLAS_PROJECT_DSN", "")
            if os.environ.get("ATLAS_PROJECT_MODE") == "1"
            or os.environ.get("ATLAS_PROJECT_DSN")
            else "",
        )
        org = organization(memory)
        result: Any
        if request.method == "GET" and not path:
            result = memory.view(org, subject)
        elif request.method == "DELETE":
            memory.delete(org, subject, path or None)
            result = dict(deleted=True)
        else:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 4096:
                    raise ProjectError(413, "request_size_exceeded")
            if path == "settings" and request.method == "PATCH":
                result = memory.setting(
                    org, subject, MemorySetting.model_validate_json(body).enabled
                )
            elif (
                request.method == "POST"
                and not path
                or request.method == "PATCH"
                and path
            ):
                result = memory.write(
                    org, subject, MemoryWrite.model_validate_json(body), path or None
                )
            else:
                raise ProjectError(404, "not_found")
        return JSONResponse(result, headers={"Cache-Control": "private, no-store"})
    except ValidationError:
        return JSONResponse(
            dict(
                error="invalid_memory",
                message="Memory text must contain 1–500 characters and a supported kind.",
            ),
            status_code=422,
        )
    except ProjectError as exc:
        return JSONResponse(exc.body, status_code=exc.status)
