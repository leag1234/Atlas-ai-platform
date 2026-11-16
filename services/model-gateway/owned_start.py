"""Power the on-demand owned server on when a strict request finds it silent.

The owned inference server may be a rented GPU powered off between uses. Its starter is
an operator service on the host, outside Atlas: it alone holds the cloud credentials and
enforces the limits (one start at a time, starts per hour, daily hours, monthly budget).
Atlas never receives those credentials. It only writes a request file in a directory the
operator gives it, and reads the starter's status file there:

    $ATLAS_OWNED_SERVER_STARTER_DIR/request      written by Atlas; its content is ignored
    $ATLAS_OWNED_SERVER_STARTER_DIR/status.json  written by the starter

status.json: ``state`` (``starting``, ``ready``, ``refused``, ``failed`` or ``stopped``),
``updated`` (Unix time of the write), and for ``starting``: ``since`` and
``eta_seconds``; for ``refused``: ``reason`` (``budget`` or ``start_limit``),
``measured``, ``limit``, ``unit``, ``period`` and ``retry_after``.

Without that setting (production, development) nothing is requested and the caller
reports its usual error. Only strict calls reach this module: shared traffic never routes
to the owned server, so it never starts it.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import aiohttp

import provider_settings
from packages.owned_server import (
    BUDGET,
    START_LIMIT,
    STARTING,
    OwnedServerUnavailable,
)

SETTING = "ATLAS_OWNED_SERVER_STARTER_DIR"
# Cold start of the staging L4 from stopped, measured 2026-11-15: 115 to 125 s.
START_SECONDS = 120
# How long a new request waits for the starter's first answer.
ANSWER_SECONDS = 3.0
# A "starting" status older than this is stale (the starter gives up at 40 minutes).
STARTING_VALID_SECONDS = 2700


def directory() -> Path | None:
    value = os.environ.get(SETTING, "").strip()
    return Path(value) if value and os.path.isabs(value) else None


async def answers() -> bool:
    """Whether the owned server lists its models within a few seconds."""
    endpoint = provider_settings.owned_endpoint()
    key = provider_settings.owned_api_key()
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5), trust_env=False
        ) as session:
            async with session.get(
                endpoint + "/models",
                headers={"Authorization": "Bearer " + key} if key else {},
                allow_redirects=False,
            ) as response:
                return response.status == 200
    except (aiohttp.ClientError, TimeoutError, OSError):
        return False


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value else None


def read_status(folder: Path) -> dict[str, Any] | None:
    try:
        value = json.loads((folder / "status.json").read_text()[:4096])
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def request_start(folder: Path) -> float | None:
    """Drop the start request; a pending one is not rewritten. None if it cannot be
    written (the starter is not installed here)."""
    now = time.time()
    try:
        handle = os.open(
            folder / "request", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
    except FileExistsError:
        return now
    except OSError:
        logging.getLogger(__name__).warning(
            json.dumps({"event": "owned_server_request_failed"})
        )
        return None
    with os.fdopen(handle, "w") as out:
        out.write(f"{int(now)}\n")
    return now


def answer(status: dict[str, Any], now: float) -> OwnedServerUnavailable | None:
    """The answer a status gives, or None (ready, failed, stopped, unreadable)."""
    state = status.get("state")
    if state == "starting":
        since = _number(status.get("since")) or now
        eta = _number(status.get("eta_seconds")) or START_SECONDS
        if now - since > STARTING_VALID_SECONDS:
            return None
        return OwnedServerUnavailable(
            STARTING, retry_after=max(10, int(since + eta - now))
        )
    if state == "refused" and status.get("reason") in ("budget", "start_limit"):
        retry = _number(status.get("retry_after"))
        unit, period = status.get("unit"), status.get("period")
        return OwnedServerUnavailable(
            BUDGET if status["reason"] == "budget" else START_LIMIT,
            retry_after=int(retry) if retry is not None and retry > 0 else None,
            measured=_number(status.get("measured")),
            limit=_number(status.get("limit")),
            unit=unit if isinstance(unit, str) else None,
            period=period if isinstance(period, str) else None,
        )
    return None


async def ensure() -> None:
    """For a strict call: return when the owned server answers or no starter is
    configured; otherwise ask the starter to power it on and raise its answer
    (starting, or a named refusal). Returns when the starter gives no usable answer, so
    that the caller reports its own error."""
    folder = directory()
    if folder is None or await answers():
        return
    log = logging.getLogger(__name__)
    status = read_status(folder)
    if status is not None and status.get("state") == "starting":
        found = answer(status, time.time())
        if found is not None:
            log.info(json.dumps({"event": "owned_server_starting", **found.fields()}))
            raise found
    requested = request_start(folder)
    if requested is None:
        return
    deadline = time.monotonic() + ANSWER_SECONDS
    while True:
        status = read_status(folder)
        updated = _number(status.get("updated")) if status is not None else None
        if status is not None and updated is not None and updated >= requested - 1:
            found = answer(status, time.time())
            log.info(
                json.dumps(
                    {
                        "event": "owned_server_start_requested",
                        "state": str(status.get("state"))[:20],
                        **(found.fields() if found is not None else {}),
                    }
                )
            )
            if found is not None:
                raise found
            return
        if time.monotonic() >= deadline:
            log.warning(json.dumps({"event": "owned_server_starter_silent"}))
            return
        await asyncio.sleep(0.1)
