"""Connecting the member's own computer: the script, its token and its two calls.

Owner requirement of 2026-11-14: Atlas works on the person's computer (and the
machines of his local network) the way a coding agent running there would.
The person pastes one command on that computer (connect.sh, readable at
/api/atlas/connect.sh). The script presents a one-time token once (enrolment)
and then announces each session it starts, signed with the computer's host key.

Both calls come from a terminal, with no browser session: the token and the
machine reference are bound to one member, one organization and one machine
by an HMAC under the instance's identity secret, and the member's own machines
API (project_machines.py) still decides everything else as that member.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

router = APIRouter()
SCRIPT = Path(__file__).resolve().parents[1] / "runner" / "connect.sh"
UPTERM_VERSION = "0.41.1"
UPTERM_DOWNLOAD = (
    "https://github.com/owenthereal/upterm/releases/download/v" + UPTERM_VERSION
)
ORIGIN = re.compile(r"https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?")
TOKEN_S = 900
LIMIT = 8192


def enabled() -> bool:
    from services.runner.relay import enabled as relay

    return relay() and os.environ.get("ATLAS_REMOTE_RUNNERS") == "1"


def origin() -> str:
    """The address the person's computer reaches this instance at."""
    value = (
        os.environ.get("ATLAS_RELAY_CONNECT_ORIGIN")
        or os.environ.get("ATLAS_PROJECT_ORIGIN")
        or ""
    ).rstrip("/")
    if not ORIGIN.fullmatch(value):
        raise ValueError("connect_origin_unset")
    return value


def _key() -> bytes:
    secret = os.environ.get("ATLAS_PROJECT_IDENTITY_SECRET", "")
    if len(secret.encode()) < 32:
        raise ValueError("identity_secret_unset")
    return hmac.new(secret.encode(), b"atlas-connect-v1", hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def seal(**claims: object) -> str:
    body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    return body + "." + _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())


def unseal(token: str, kind: str) -> dict[str, Any]:
    """The claims of a token this instance issued, or ValueError."""
    if not isinstance(token, str) or len(token) > 2048 or token.count(".") != 1:
        raise ValueError("invalid_token")
    body, mac = token.split(".")
    expected = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, expected):
        raise ValueError("invalid_token")
    claims = json.loads(_unb64(body))
    if not isinstance(claims, dict) or claims.get("k") != kind:
        raise ValueError("invalid_token")
    UUID(str(claims["o"])), UUID(str(claims["m"]))
    if not isinstance(claims.get("s"), str) or not claims["s"]:
        raise ValueError("invalid_token")
    if "e" in claims and int(claims["e"]) < time.time():
        raise ValueError("connect_token_expired_or_used")
    return claims


def enrolment_token(subject: str, org: str, machine: str, nonce: str) -> str:
    return seal(
        k="enrol", s=subject, o=org, m=machine, n=nonce, e=int(time.time()) + TOKEN_S
    )


def connect_command(token: str) -> str:
    """The one line the person pastes on his computer, in his project folder."""
    base = origin()
    return (
        "mkdir -p ~/.atlas && curl -fsSL "
        + base
        + "/api/atlas/connect.sh -o ~/.atlas/connect.sh && sh ~/.atlas/connect.sh "
        + base
        + " "
        + token
    )


def known_host(url: str, key: str) -> str:
    """The relay line of the computer's known_hosts, for the address it dials."""
    parts = urlsplit(url)
    default = {"wss": 443, "ws": 80, "ssh": 22}[parts.scheme]
    return f"[{parts.hostname}]:{parts.port or default} {key}"


async def _body(request: Request) -> dict[str, Any]:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > LIMIT:
            raise ValueError("request_too_large")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("invalid_request")
    return data


async def _as_member(
    claims: dict[str, Any], tail: str, data: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    from services.orchestrator.code_api import ExecutionBroker

    broker = ExecutionBroker(claims["s"], str(claims["o"]), "")
    return await broker.member(
        "POST", f"machines/{claims['m']}/{tail}", data, timeout=90
    )


def _refused(error: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        dict(error=error), status_code=status, headers={"Cache-Control": "no-store"}
    )


@router.get("/api/atlas/connect.sh")
async def script() -> Response:
    if not enabled():
        return _refused("not_found", 404)
    return PlainTextResponse(
        SCRIPT.read_text(),
        media_type="text/x-shellscript",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/api/atlas/connect/enrol")
async def enrol(request: Request) -> Response:
    if not enabled():
        return _refused("not_found", 404)
    from services.retrieval.project_identity import ProjectError
    from services.runner.relay import relay_key

    try:
        data = await _body(request)
        claims = unseal(str(data.pop("token", "")), "enrol")
        status, body = await _as_member(claims, "enrol", dict(data, nonce=claims["n"]))
        if status != 200:
            return _refused(str(body.get("error", "connect_refused")), status)
        relay_url = os.environ["ATLAS_RELAY_PUBLIC_URL"]
        key = relay_key()
        return JSONResponse(
            dict(
                machine=seal(k="machine", s=claims["s"], o=claims["o"], m=claims["m"]),
                machine_id=str(claims["m"]),
                guest_key=body["guest_key"],
                relay_url=relay_url,
                relay_known_host=known_host(relay_url, key),
                upterm_version=UPTERM_VERSION,
                upterm_download=os.environ.get("ATLAS_RELAY_DOWNLOAD", UPTERM_DOWNLOAD),
            ),
            headers={"Cache-Control": "no-store"},
        )
    except ProjectError as exc:
        return _refused(str(exc.body.get("error", "connect_refused")), exc.status)
    except (ValueError, KeyError, TypeError) as exc:
        return _refused(
            str(exc) if str(exc).startswith("connect_") else "invalid_request"
        )


@router.post("/api/atlas/connect/announce")
async def announce(request: Request) -> Response:
    if not enabled():
        return _refused("not_found", 404)
    from services.retrieval.project_identity import ProjectError

    try:
        data = await _body(request)
        claims = unseal(str(data.pop("machine", "")), "machine")
        status, body = await _as_member(
            claims,
            "announce",
            dict(message=str(data["message"]), signature=str(data["signature"])),
        )
        return JSONResponse(
            body, status_code=status, headers={"Cache-Control": "no-store"}
        )
    except ProjectError as exc:
        return _refused(str(exc.body.get("error", "announce_refused")), exc.status)
    except (ValueError, KeyError, TypeError):
        return _refused("invalid_request")
