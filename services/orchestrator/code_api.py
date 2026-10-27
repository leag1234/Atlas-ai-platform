"""Owner-only audit retrieval and approval answers."""

from pathlib import Path
from typing import Any
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from services.orchestrator.code_tools import gate
from services.orchestrator.code_workspace import enabled, session_key, state_dir

router = APIRouter()


@router.get("/code/ui")
async def configuration() -> Response:
    if not enabled():
        return JSONResponse({"enabled": False}, status_code=404)
    from services.orchestrator.code_mode import code_profile

    return JSONResponse(
        {"enabled": True, "profile": code_profile()},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/code/sessions/{session}/status")
async def session_status(session: str) -> Response:
    if not enabled():
        return JSONResponse({"error": "code_mode_disabled"}, status_code=404)
    from services.orchestrator.code_mode import active, active_lock

    try:
        session_key(session)
    except ValueError:
        return JSONResponse({"error": "invalid_session"}, status_code=400)
    with active_lock:
        running = session in active
    return JSONResponse({"running": running}, headers={"Cache-Control": "no-store"})


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    allow: bool


@router.post("/code/approvals/{key}")
async def answer(key: str, decision: Decision, request: Request) -> Response:
    if not enabled():
        return JSONResponse({"error": "code_mode_disabled"}, status_code=404)
    if request.headers.get("origin") not in (None, str(request.base_url).rstrip("/")):
        return JSONResponse({"error": "origin_rejected"}, status_code=403)
    if not gate.answer(key, decision.allow):
        return JSONResponse(
            {"error": "approval_expired_or_already_answered"}, status_code=410
        )
    return JSONResponse({"accepted": True})


@router.get("/code/sessions/{session}/audit")
async def audit(session: str) -> Response:
    if not enabled():
        return JSONResponse({"error": "code_mode_disabled"}, status_code=404)
    try:
        path: Path = state_dir() / (session_key(session) + ".jsonl")
        content = path.read_bytes()
    except (ValueError, FileNotFoundError):
        return JSONResponse({"error": "code_session_not_found"}, status_code=404)
    return Response(
        content,
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store"},
    )


class ExecutionBroker:
    """Service client: all authority and durable state remain in retrieval."""

    private_prefix = "/internal/atlas/execution/"
    success_statuses: tuple[int, ...] = (200,)

    def __init__(self, subject: str, org: str, project: str) -> None:
        self.subject, self.org, self.project = subject, org, project

    async def call(
        self, operation: str, data: dict[str, Any], key: str | None = None
    ) -> dict[str, Any]:
        import os
        from uuid import uuid4
        import aiohttp
        from services.orchestrator.project_ui import broker_identity, bounded_json
        from services.orchestrator.chat_pipeline import retrieval_url
        from services.retrieval.project_identity import ProjectError

        secret = os.environ.get("ATLAS_PROJECT_EXECUTION_SECRET", "")
        if len(secret.encode()) < 32:
            raise ProjectError(503, "dependency_unavailable")
        try:
            async with aiohttp.ClientSession(
                trust_env=False,
                timeout=aiohttp.ClientTimeout(
                    total=55 if operation == "remote" else 10
                ),
            ) as client:
                resolved = await broker_identity(
                    client, self.subject, self.org, self.project
                )
                async with client.post(
                    retrieval_url() + self.private_prefix + operation,
                    json=data,
                    headers={
                        "X-Atlas-Project-Identity": str(resolved["identity"]),
                        "X-Atlas-Execution-Secret": secret,
                        "X-Atlas-Execution-Audience": "atlas-retrieval-execution",
                        "Idempotency-Key": key or str(uuid4()),
                    },
                    allow_redirects=False,
                ) as reply:
                    result = await bounded_json(reply)
                    if reply.status not in self.success_statuses:
                        error = ProjectError(
                            reply.status,
                            str(result.get("error", "dependency_unavailable")),
                        )
                        error.body = result
                        raise error
                    return result
        except (aiohttp.ClientError, TimeoutError):
            raise ProjectError(503, "dependency_unavailable") from None

    async def public(
        self,
        method: str,
        tail: str,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        import aiohttp
        from services.orchestrator.project_ui import broker_identity, bounded_json
        from services.orchestrator.chat_pipeline import retrieval_url
        from services.retrieval.project_identity import ProjectError

        try:
            async with aiohttp.ClientSession(
                trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
            ) as client:
                resolved = await broker_identity(
                    client, self.subject, self.org, self.project
                )
                async with client.request(
                    method,
                    retrieval_url()
                    + f"/api/atlas/orgs/{self.org}/projects/{self.project}/"
                    + tail,
                    data=body,
                    headers={
                        **(headers or {}),
                        "X-Atlas-Project-Identity": str(resolved["identity"]),
                    },
                    allow_redirects=False,
                ) as reply:
                    return reply.status, await bounded_json(reply)
        except (aiohttp.ClientError, TimeoutError):
            raise ProjectError(503, "dependency_unavailable") from None

    async def content(self, file_id: str, version_id: str, limit: int) -> bytes:
        import aiohttp
        from services.orchestrator.project_ui import broker_identity
        from services.orchestrator.chat_pipeline import retrieval_url
        from services.retrieval.project_identity import ProjectError

        async with aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=30)
        ) as client:
            resolved = await broker_identity(
                client, self.subject, self.org, self.project
            )
            async with client.get(
                retrieval_url()
                + f"/api/atlas/orgs/{self.org}/projects/{self.project}/files/{file_id}/content",
                params={"version": version_id},
                headers={"X-Atlas-Project-Identity": str(resolved["identity"])},
                allow_redirects=False,
            ) as reply:
                if reply.status != 200:
                    raise ProjectError(
                        reply.status,
                        "not_found"
                        if reply.status == 404
                        else "dependency_unavailable",
                    )
                data = bytearray()
                async for chunk in reply.content.iter_chunked(65536):
                    if len(data) + len(chunk) > limit:
                        raise ProjectError(
                            413,
                            "size_limit",
                            f"snapshot: measured {len(data) + len(chunk)} bytes; limit {limit} bytes",
                            measurements=[
                                dict(
                                    measured=len(data) + len(chunk),
                                    limit=limit,
                                    unit="bytes",
                                )
                            ],
                        )
                    data.extend(chunk)
                return bytes(data)


WORK_PREFIX = "/api/atlas/orgs/{org}/projects/{project}"


@router.post(WORK_PREFIX + "/work-sessions")
@router.get(WORK_PREFIX + "/work-sessions/{session}")
@router.post(WORK_PREFIX + "/work-sessions/{session}/cancel")
@router.post(WORK_PREFIX + "/approvals/{approval}/decision")
async def scoped_work_route(
    request: Request,
    org: str,
    project: str,
    session: str | None = None,
    approval: str | None = None,
) -> Response:
    import asyncio
    import aiohttp
    from services.orchestrator.code_ui import project_mode
    from services.orchestrator.project_ui import (
        browser_origin,
        verified_subject,
        selection,
        public_error,
    )
    from services.retrieval.project_identity import ProjectError

    if not project_mode():
        return public_error(ProjectError(409, "feature_not_available"))
    try:
        browser_origin(request)
        path = request.url.path.removeprefix("/api/atlas/")
        selection(path)
        async with aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
        ) as client:
            subject = await verified_subject(request, client)
        broker = ExecutionBroker(subject, org, project)
        # Reauthorize the selected project before accepting even a small body.
        from services.orchestrator.project_ui import broker_identity

        async with aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
        ) as client:
            await broker_identity(client, subject, org, project)
        body = bytearray()
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                measured = len(body) + len(chunk)
                if measured > 160000:
                    raise ProjectError(
                        413,
                        "size_limit",
                        f"Work request: measured {measured} bytes; limit 160000 bytes",
                        measurements=[
                            dict(measured=measured, limit=160000, unit="bytes")
                        ],
                    )
                body.extend(chunk)
        tail = path.split("/", 4)[4]
        if request.url.query:
            tail += "?" + request.url.query
        status, result = await broker.public(
            request.method,
            tail,
            bytes(body),
            {
                k: request.headers[k]
                for k in ("idempotency-key", "if-match", "content-type")
                if k in request.headers
            },
        )
        if (
            status == 202
            and session is not None
            and request.url.path.endswith("/cancel")
        ):
            from services.orchestrator.code_mode import cleanup_work

            import json

            await cleanup_work(broker, session, str(json.loads(body)["request_id"]))
        response_headers = {"Cache-Control": "private, no-store"}
        if "revision" in result:
            response_headers["ETag"] = f'"{result["revision"]}"'
        return JSONResponse(result, status_code=status, headers=response_headers)
    except ProjectError as exc:
        return public_error(exc)
    except (aiohttp.ClientError, TimeoutError, ValueError, OSError, RuntimeError):
        return public_error(ProjectError(503, "dependency_unavailable"))
