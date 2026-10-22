"""Same-origin local project UI and bounded proxy to retrieval-owned APIs."""

from packages.limits import LimitError
from packages.ports import atlas_ports
from services.retrieval.project_identity import ProjectError

import asyncio
import hmac
import json
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4
from pathlib import Path

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from services.orchestrator.chat_pipeline import retrieval_url
from services.orchestrator.code_ui import project_mode, legacy_project_route
from starlette.types import ASGIApp, Scope, Receive, Send

router = APIRouter()
ROOT = Path(__file__).parent
CSP = "default-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"


@router.get("/project-ui")
def ui() -> Response:
    return HTMLResponse(
        (ROOT / "project_ui.html").read_text(), headers={"Content-Security-Policy": CSP}
    )


@router.get("/project-ui.js")
def javascript() -> Response:
    return Response((ROOT / "project_ui.js").read_text(), media_type="text/javascript")


@router.api_route("/projects", methods=["GET", "POST"])
@router.api_route("/projects/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def proxy(request: Request, path: str = "") -> Response:
    if project_mode():
        return public_error(ProjectError(404, "not_found"))
    if request.headers.get("origin") not in (None, str(request.base_url).rstrip("/")):
        return JSONResponse({"error": "origin_rejected"}, status_code=403)
    if any(part in {".", ".."} for part in path.split("/")) or any(
        c in path for c in "?#!\\"
    ):
        return JSONResponse({"error": "invalid_path"}, status_code=400)
    try:
        data = bytearray()
        async with asyncio.timeout(35):
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 160000:
                    raise LimitError("body_limit", len(data), 160000, "bytes")
            async with aiohttp.ClientSession(trust_env=False) as client:
                async with client.request(
                    request.method,
                    retrieval_url() + "/projects" + ("/" + path if path else ""),
                    data=bytes(data),
                    headers={"Content-Type": "application/json"},
                    allow_redirects=False,
                ) as reply:
                    result = bytearray()
                    async for chunk in reply.content.iter_chunked(16384):
                        result.extend(chunk)
                        if len(result) > 800000:
                            raise LimitError(
                                "response_limit", len(result), 800000, "bytes"
                            )
                    return Response(
                        bytes(result),
                        status_code=reply.status,
                        media_type="application/json",
                    )
    except LimitError as exc:
        return JSONResponse({"error": exc.code, "message": exc.detail}, status_code=413)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return JSONResponse({"error": "project_unavailable"}, status_code=503)


def public_error(exc: ProjectError) -> JSONResponse:
    return JSONResponse(
        exc.body, status_code=exc.status, headers={"Cache-Control": "private, no-store"}
    )


def browser_origin(request: Request) -> None:
    expected = os.environ.get("ATLAS_PROJECT_ORIGIN") or str(request.base_url).rstrip(
        "/"
    )
    origin = request.headers.get("origin")
    site = request.headers.get("sec-fetch-site")
    if (
        (site is not None and site not in {"same-origin", "none"})
        or (origin is not None and origin != expected)
        or (request.method not in {"GET", "HEAD", "OPTIONS"} and origin != expected)
    ):
        raise ProjectError(403, "origin_rejected")


async def bounded_json(reply: aiohttp.ClientResponse) -> dict[str, Any]:
    body = bytearray()
    async for chunk in reply.content.iter_chunked(16384):
        body.extend(chunk)
        if len(body) > 160000:
            raise ProjectError(503, "dependency_unavailable")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ProjectError(503, "dependency_unavailable")
    return value


async def verified_subject(request: Request, client: aiohttp.ClientSession) -> str:
    trusted = os.environ.get("ATLAS_TRUSTED_EMAIL_HEADER", "X-Atlas-User").lower()
    forbidden = {
        "x-atlas-project-identity",
        "x-atlas-identity-secret",
        "x-atlas-execution-secret",
        "x-atlas-execution-audience",
        "x-atlas-user-role",
    }
    if any(name in request.headers for name in forbidden):
        raise ProjectError(401, "unauthenticated")
    if trusted in request.headers or "x-atlas-proxy-secret" in request.headers:
        secret = os.environ.get("ATLAS_PROXY_SECRET", "")
        if (
            not secret
            or len(request.headers.getlist(trusted)) != 1
            or len(request.headers.getlist("x-atlas-proxy-secret")) != 1
            or not hmac.compare_digest(
                request.headers.get("x-atlas-proxy-secret", "").encode(),
                secret.encode(),
            )
        ):
            raise ProjectError(401, "unauthenticated")
        subject = request.headers[trusted]
    else:
        token = request.cookies.get("token", "")
        if not token or len(token) > 8192 or any(c in token for c in "\r\n"):
            raise ProjectError(401, "unauthenticated")
        url = os.environ.get("ATLAS_WEBUI_URL") or atlas_ports().url("chat_ui")
        async with client.get(
            url.rstrip("/") + "/api/v1/auths/",
            headers={"Authorization": "Bearer " + token},
            allow_redirects=False,
        ) as reply:
            if reply.status in {401, 403}:
                raise ProjectError(401, "unauthenticated")
            if reply.status != 200:
                raise ProjectError(503, "dependency_unavailable")
            user = await bounded_json(reply)
        # The front door asserts email; use the same verified host field so both
        # supported authentication mechanisms resolve the same membership.
        subject = user.get("email", "")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 320:
        raise ProjectError(401, "unauthenticated")
    return subject


def selection(path: str) -> tuple[str | None, str | None]:
    if path == "me":
        return None, None
    parts = path.split("/")
    if len(parts) < 3 or parts[0] != "orgs":
        raise ProjectError(404, "not_found")
    if any(part in {"", ".", ".."} for part in parts) or any(
        c in path for c in "\\%?#"
    ):
        raise ProjectError(404, "not_found")
    try:
        org = str(UUID(parts[1]))
        project = (
            str(UUID(parts[3])) if len(parts) > 3 and parts[2] == "projects" else None
        )
    except ValueError:
        raise ProjectError(404, "not_found") from None
    return org, project


async def broker_identity(
    client: aiohttp.ClientSession, subject: str, org: str | None, project: str | None
) -> dict[str, Any]:
    secret = os.environ.get("ATLAS_PROJECT_IDENTITY_SECRET", "")
    if len(secret.encode()) < 32:
        raise ProjectError(503, "dependency_unavailable")
    async with client.post(
        retrieval_url() + "/internal/atlas/identity",
        json=dict(subject=subject, org=org, project=project),
        headers={"X-Atlas-Identity-Secret": secret, "Idempotency-Key": str(uuid4())},
        allow_redirects=False,
    ) as reply:
        result = await bounded_json(reply)
        if reply.status != 200:
            if reply.status in {401, 403, 404, 422}:
                raise ProjectError(
                    reply.status, str(result.get("error", "unauthenticated"))
                )
            raise ProjectError(503, "dependency_unavailable")
    return result


@router.api_route(
    "/api/atlas/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
)
async def scoped_proxy(request: Request, path: str) -> Response:
    if not project_mode():
        return public_error(ProjectError(409, "feature_not_available"))
    client = aiohttp.ClientSession(
        trust_env=False,
        auto_decompress=False,
        timeout=aiohttp.ClientTimeout(total=300, connect=5, sock_read=35),
    )
    reply: aiohttp.ClientResponse | None = None
    try:
        browser_origin(request)
        subject = await verified_subject(request, client)
        if path == "me" and request.method != "GET":
            raise ProjectError(404, "not_found")
        org, project = selection(path)
        resolved = await broker_identity(client, subject, org, project)
        if path == "me":
            await client.close()
            return JSONResponse(
                dict(
                    memberships=resolved["memberships"],
                    capabilities=dict(
                        chat=True,
                        work=True,
                        scheduled_tasks=False,
                        skills_execute=False,
                        local_reads=False,
                        sync=False,
                    ),
                ),
                headers={"Cache-Control": "private, no-store"},
            )
        token = resolved.get("identity")
        if not isinstance(token, str) or not token:
            raise ProjectError(503, "dependency_unavailable")
        headers = {
            name: request.headers[name]
            for name in (
                "content-type",
                "idempotency-key",
                "if-match",
                "range",
                "if-range",
                "last-event-id",
            )
            if name in request.headers
        }
        headers["X-Atlas-Project-Identity"] = token
        reply = await client.request(
            request.method,
            retrieval_url() + "/api/atlas/" + path,
            params=list(request.query_params.multi_items()),
            data=request.stream(),
            headers=headers,
            allow_redirects=False,
        )
        if 300 <= reply.status < 400:
            raise ProjectError(503, "dependency_unavailable")
        response_headers = {
            name: reply.headers[name]
            for name in (
                "Content-Type",
                "Content-Disposition",
                "ETag",
                "Accept-Ranges",
                "Content-Range",
                "Content-Length",
            )
            if name in reply.headers
        }
        response_headers["Cache-Control"] = "private, no-store"
        upstream = reply

        async def content() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.content.iter_chunked(65536):
                    yield chunk
            finally:
                upstream.close()
                await client.close()

        return StreamingResponse(
            content(), status_code=reply.status, headers=response_headers
        )
    except ProjectError as exc:
        result: Response = public_error(exc)
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError):
        result = public_error(ProjectError(503, "dependency_unavailable"))
    if reply is not None:
        reply.close()
    await client.close()
    return result


@router.api_route(
    "/internal/atlas/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
)
async def private_route(path: str) -> Response:
    return public_error(ProjectError(404, "not_found"))


class ProjectGate:
    """Apply legacy gates even to direct adapter requests outside the UI relay."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and project_mode()
            and legacy_project_route(scope["path"])
        ):
            await public_error(ProjectError(404, "not_found"))(scope, receive, send)
            return
        await self.app(scope, receive, send)
