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
``measured``, ``limit``, ``unit``, ``period`` and ``retry_after``; for ``failed``:
``reason`` (``capacity`` when the hosting provider had no machine of that type free,
else absent or ``start_failed``).

D5: the user reads the real outcome. A start the cloud refuses fails within seconds, so
"starting" is answered only for a start at least CONFIRM_SECONDS old, whether this request
asked for it or found it in flight (one chat turn makes several strict calls: the second
read "starting" while the first was still watching, staging 2026-11-17); a failed
start is answered as such (``owned_server_unavailable``) for FAILED_HOLD_SECONDS, without
asking for another start, which would fail the same way and use up the hourly starts
(staging 2026-11-17: four strict requests in 15 s asked for four starts, each refused by
the provider for lack of stock, and each request read "starting").

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
    UNAVAILABLE,
    OwnedServerUnavailable,
)

SETTING = "ATLAS_OWNED_SERVER_STARTER_DIR"
# Cold start of the staging L4 from stopped, measured 2026-11-15: 115 to 125 s.
START_SECONDS = 120
# How long a new request waits for the starter's first answer.
ANSWER_SECONDS = 3.0
# A "starting" status older than this is stale (the starter gives up at 40 minutes).
STARTING_VALID_SECONDS = 2700
# How long a new start is watched for a quick refusal before "starting" is answered
# (the provider refused for lack of stock 2 s after the start, staging 2026-11-17).
CONFIRM_SECONDS = 8.0
# After a failed start, requests get that failure for this long instead of a new start.
FAILED_HOLD_SECONDS = 300


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


def failure(status: dict[str, Any], now: float) -> OwnedServerUnavailable | None:
    """A recent failed start, answered as such; None once FAILED_HOLD_SECONDS passed."""
    if status.get("state") != "failed":
        return None
    updated = _number(status.get("updated"))
    if updated is None or not 0 <= now - updated < FAILED_HOLD_SECONDS:
        return None
    reason = status.get("reason")
    return OwnedServerUnavailable(
        UNAVAILABLE,
        retry_after=max(60, int(updated + FAILED_HOLD_SECONDS - now)),
        reason=reason if isinstance(reason, str) else None,
    )


async def confirmed(
    folder: Path, found: OwnedServerUnavailable | None, since: float
) -> OwnedServerUnavailable | None:
    """A "starting" answer for a start younger than CONFIRM_SECONDS is held back while
    the status is watched: a quick refusal (or the server being ready) replaces it."""
    while (
        found is not None
        and found.code == STARTING
        and time.time() - since < CONFIRM_SECONDS
    ):
        await asyncio.sleep(0.1)
        status = read_status(folder)
        if status is not None:
            found = answer(status, time.time())
    return found


def answer(status: dict[str, Any], now: float) -> OwnedServerUnavailable | None:
    """The answer a status gives, or None (ready, stopped, unreadable, or a failure
    older than FAILED_HOLD_SECONDS)."""
    state = status.get("state")
    if state == "failed":
        return failure(status, now)
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
    if status is not None and status.get("state") in ("starting", "failed"):
        found = answer(status, time.time())
        if found is not None:
            since = _number(status.get("since")) or _number(status.get("updated"))
            found = await confirmed(folder, found, since or time.time())
            if found is None:
                return  # the server became ready meanwhile
            log.info(json.dumps({"event": found.code, **found.fields()}))
            raise found
    requested = request_start(folder)
    if requested is None:
        return
    deadline = time.monotonic() + ANSWER_SECONDS
    while True:
        status = read_status(folder)
        updated = _number(status.get("updated")) if status is not None else None
        if status is not None and updated is not None and updated >= requested - 1:
            # "Starting" is answered only once the start survived its first seconds:
            # a refusal by the cloud provider comes back within them.
            found = await confirmed(folder, answer(status, time.time()), requested)
            state = (read_status(folder) or status).get("state")
            log.info(
                json.dumps(
                    {
                        "event": "owned_server_start_requested",
                        "state": str(state)[:20],
                        **(found.fields() if found is not None else {}),
                    }
                )
            )
            if found is not None:
                raise found
            if state == "failed":
                # An older starter without a hold: still the real outcome.
                raise OwnedServerUnavailable(
                    UNAVAILABLE, retry_after=FAILED_HOLD_SECONDS
                )
            return
        if time.monotonic() >= deadline:
            log.warning(json.dumps({"event": "owned_server_starter_silent"}))
            return
        await asyncio.sleep(0.1)
