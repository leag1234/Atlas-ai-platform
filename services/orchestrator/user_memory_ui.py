"""Authenticated personal-memory controls for the injected Atlas UI."""

from pathlib import Path
from uuid import UUID

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import Response, JSONResponse
from pydantic import ValidationError

from packages.user_memory import MemorySetting, MemoryWrite
from services.orchestrator.project_ui import browser_origin, bounded_json, public_error
from services.orchestrator.user_memory import exchange
from services.retrieval.project_identity import ProjectError
from packages.ports import atlas_ports
import os

router = APIRouter()


@router.get("/api/atlas/user-memory/client.js")
def asset() -> Response:
    return Response(
        Path(__file__).with_name("user_memory.js").read_text(),
        media_type="text/javascript",
    )


async def browser_user(request: Request) -> str:
    browser_origin(request)
    token = request.cookies.get("token", "")
    if not token or len(token) > 8192 or any(c in token for c in "\r\n"):
        raise ProjectError(401, "unauthenticated")
    url = os.environ.get("ATLAS_WEBUI_URL") or atlas_ports().url("chat_ui")
    async with aiohttp.ClientSession(
        trust_env=False, timeout=aiohttp.ClientTimeout(total=5)
    ) as client:
        async with client.get(
            url.rstrip("/") + "/api/v1/auths/",
            headers={"Authorization": "Bearer " + token},
            allow_redirects=False,
        ) as reply:
            if reply.status != 200:
                raise ProjectError(401, "unauthenticated")
            user = await bounded_json(reply)
    subject = user.get("id")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 200:
        raise ProjectError(401, "unauthenticated")
    return subject


@router.api_route("/api/atlas/user-memory", methods=["GET", "DELETE"])
@router.api_route("/api/atlas/user-memory/{path}", methods=["PATCH", "DELETE"])
async def control(request: Request, path: str = "") -> Response:
    try:
        subject = await browser_user(request)
        if path and path != "settings":
            path = str(UUID(path))
        data = None
        if request.method == "PATCH":
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 4096:
                    raise ProjectError(413, "request_size_exceeded")
            schema = MemorySetting if path == "settings" else MemoryWrite
            data = schema.model_validate_json(body).model_dump()
        result = await exchange(subject, request.method, path, data)
        return JSONResponse(result, headers={"Cache-Control": "private, no-store"})
    except (ValidationError, ValueError):
        return public_error(ProjectError(422, "invalid_memory"))
    except ProjectError as exc:
        return public_error(exc)
    except (aiohttp.ClientError, TimeoutError):
        return public_error(ProjectError(503, "memory_unavailable"))
