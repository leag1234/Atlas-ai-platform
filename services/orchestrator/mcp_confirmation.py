"""Local human confirmation, bound to immutable arguments and configuration."""

from packages.limits import LimitError
from packages.ports import atlas_ports

import asyncio
from dataclasses import dataclass
import hashlib
import html
import json
import secrets
import threading
import time
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from services.orchestrator import mcp_client as mcp
from services.orchestrator.mcp_transport import invoke

router = APIRouter()


@dataclass(frozen=True)
class Pending:
    payload: str
    fingerprint: str
    csrf: str
    expires: float


pending: dict[str, Pending] = {}
lock = threading.Lock()


def fingerprint(server: mcp.Server, env: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps([server.model_dump(), env], sort_keys=True).encode()
    ).hexdigest()


def prepare(call: mcp.MCPCall) -> dict[str, object]:
    server, _, env, _ = mcp.resolve(call)
    digest = fingerprint(server, env)
    with lock:
        for key in list(pending):
            if pending[key].expires <= time.time():
                del pending[key]
        if len(pending) >= 100:
            raise LimitError(
                "mcp_pending_limit", len(pending) + 1, 100, "pending confirmations"
            )
        key = secrets.token_hex(24)
        pending[key] = Pending(
            call.model_dump_json(), digest, secrets.token_hex(24), time.time() + 600
        )
    return {
        "error": "confirmation_required",
        "confirmation_url": atlas_ports().url("adapter", "localhost")
        + "/mcp/confirm/"
        + key,
    }


def lookup(key: str) -> Pending:
    action = pending.get(key)
    if action is None or action.expires <= time.time():
        raise ValueError("mcp_confirmation_expired")
    return action


def local(request: Request) -> bool:
    return request.url.hostname in {"localhost", "127.0.0.1", "::1"}


@router.get("/mcp/confirm/{key}")
def preview(request: Request, key: str) -> Response:
    if not local(request):
        return JSONResponse({"error": "origin_rejected"}, status_code=403)
    try:
        with lock:
            action = lookup(key)
        response = HTMLResponse(
            '<meta charset="utf-8"><title>Confirm an ATLAS action</title>'
            "<h1>Confirm this action</h1><pre>"
            + html.escape(action.payload)
            + '</pre><form method="post"><input type="hidden" name="csrf" value="'
            + action.csrf
            + '"><button>Confirm and execute once</button></form>',
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            },
        )
        response.set_cookie(
            "mcp_csrf",
            action.csrf,
            httponly=True,
            samesite="strict",
            path="/mcp/confirm/" + key,
            max_age=600,
        )
        return response
    except ValueError:
        return JSONResponse({"error": "confirmation_unavailable"}, status_code=404)


@router.post("/mcp/confirm/{key}")
async def confirm(request: Request, key: str) -> Response:
    if not local(request) or request.headers.get("origin") != str(
        request.base_url
    ).rstrip("/"):
        return JSONResponse({"error": "origin_rejected"}, status_code=403)
    call, started, state = None, time.monotonic(), "write_failed_or_uncertain"
    try:
        if request.headers.get("content-type") != "application/x-www-form-urlencoded":
            raise ValueError("mcp_form_required")
        body = bytearray()
        async with asyncio.timeout(2):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1024:
                    raise LimitError("mcp_form_limit", len(body), 1024, "bytes")
        form = parse_qs(body.decode(), strict_parsing=True, max_num_fields=1)
        with lock:
            action = lookup(key)
            if form.get("csrf") != [action.csrf] or not secrets.compare_digest(
                request.cookies.get("mcp_csrf", ""), action.csrf
            ):
                raise ValueError("mcp_confirmation_required")
            del pending[key]
        candidate = mcp.MCPCall.model_validate_json(action.payload)
        server, tool, env, credentials = mcp.resolve(candidate)
        if fingerprint(server, env) != action.fingerprint:
            raise ValueError("mcp_configuration_changed")
        if tool.effect != "write":
            raise ValueError("mcp_write_required")
        call = candidate
        result = await invoke(
            server.command, server.args, env, credentials, call.tool, call.arguments, 15
        )
        state = "write_ok"
        return JSONResponse({"trust": "untrusted", "data": result})
    except LimitError as exc:
        return JSONResponse({"error": exc.code, "message": exc.detail}, status_code=400)
    except Exception:
        return JSONResponse(
            {"error": "confirmation_refused_or_write_uncertain"}, status_code=400
        )
    finally:
        if call is not None:
            mcp.audit(call, state, started)
