"""Wait for the on-demand owned server instead of failing the request (strict privacy).

A strict request that finds the owned server powered off starts it (owned_start.py);
the cold start takes about two minutes (115 to 125 s for the staging L4, measured
2026-11-15). The request then waits for it and is answered as soon as it is up, instead
of failing with "starting" and asking the person to send the message again (staging
2026-11-20: a strict chat turn showed "send it again in a moment" under an
interrupted answer with a Retry button).

The wait is bounded (WAIT_SECONDS) and ends at once on the starter's real outcome: a
refusal (budget cap, starts per hour) or a failed start is answered as such (D5), and
after the limit the request gets "starting" with the seconds it waited. Nothing is ever
sent to another provider meanwhile: the same strict call is made again once the server
answers, and nothing else.

- Chat turns (GatewayModel): the turn's status line reads "Starting the private
  server… 45 s / ~2 min" (``phase: owned_server_starting``), the turn deadline is moved
  by the time waited (deadline.infrastructure_startup), the stream stays alive through
  the status heartbeat.
- API requests (dev_inference.py): a streamed answer sends SSE comments while it waits;
  a request that is not streamed waits silently. ``X-Atlas-Owned-Wait: 0`` (or any
  number of seconds up to WAIT_SECONDS) asks for a shorter wait: 0 is the former
  immediate 503 with Retry-After.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextvars import ContextVar
from typing import Any, TypeVar

from packages.owned_server import STARTING, OwnedServerUnavailable, wait_text

T = TypeVar("T")

# Longest wait for a start: twice the measured cold start of the staging L4.
WAIT_SECONDS = 240
# How often the server and the starter's status are read while waiting.
POLL_SECONDS = 3.0
# A cold start's expected length when the starter gives none (owned_start.START_SECONDS).
EXPECTED_SECONDS = 120
# Request header: the longest wait the API client accepts, in seconds (0: none).
WAIT_HEADER = "X-Atlas-Owned-Wait"
# The wait the current request accepts (set from WAIT_HEADER by the API entry points).
accepted: ContextVar[int | None] = ContextVar("owned_wait_accepted", default=None)

_STARTING = {
    "fr": "Démarrage du serveur privé…",
    "en": "Starting the private server…",
    "de": "Der private Server wird gestartet…",
    "es": "Iniciando el servidor privado…",
    "it": "Avvio del server privato…",
}


def status_text(language: str, waited: float, expected: float) -> str:
    """The status line of a chat turn waiting for the owned server."""
    label = _STARTING.get(language, _STARTING["en"])
    expected = max(expected, waited + 1)
    return f"{label} {int(waited)} s / ~{wait_text(int(expected))}"


def requested_wait(headers: Mapping[str, str]) -> int:
    """The wait an API client accepts: WAIT_SECONDS unless it asks for less."""
    value = (headers.get(WAIT_HEADER) or "").strip()
    if not value:
        return WAIT_SECONDS
    try:
        seconds = int(value)
    except ValueError:
        return WAIT_SECONDS
    return max(0, min(WAIT_SECONDS, seconds))


def request_wait(request: Any) -> int:
    """The wait a request accepts: read by the API edge before it strips the client's
    X-Atlas headers (api_key_edge.py), else from the request's own headers."""
    chosen = getattr(request.state, "owned_wait", None)
    if isinstance(chosen, int) and not isinstance(chosen, bool):
        return max(0, min(WAIT_SECONDS, chosen))
    return requested_wait(request.headers)


async def waiting(
    first: OwnedServerUnavailable, limit: float = WAIT_SECONDS
) -> AsyncIterator[OwnedServerUnavailable]:
    """Yield the latest "starting" answer while the owned server starts, about every
    POLL_SECONDS; return once it answers, or once no starter gives an answer (the next
    call then reports its own error). Raise the starter's refusal or failure as soon as
    it comes, and "starting" with ``waited`` once ``limit`` seconds have passed."""
    import owned_start

    started = time.monotonic()
    answer = first
    while True:
        waited = time.monotonic() - started
        if waited >= limit:
            raise OwnedServerUnavailable(
                STARTING, retry_after=answer.retry_after, waited=max(1, int(waited))
            )
        yield answer
        await asyncio.sleep(max(0.0, min(POLL_SECONDS, limit - waited)))
        try:
            await owned_start.ensure()
        except OwnedServerUnavailable as exc:
            if exc.code != STARTING:
                raise
            answer = exc
            continue
        return


Sink = Callable[[dict[str, object]], Awaitable[None]]


async def call(
    attempt: Callable[[], Awaitable[T]],
    sink: Sink | None = None,
    limit: float | None = None,
) -> T:
    """Make a gateway call; when it finds the owned server starting, wait for the
    server (status on ``sink``, else on the turn's stream sink) and make it again."""
    from services.orchestrator.deadline import infrastructure_startup
    from services.orchestrator.loop import OwnedServerAnswer

    if limit is None:
        chosen = accepted.get()
        limit = WAIT_SECONDS if chosen is None else chosen
    try:
        return await attempt()
    except OwnedServerAnswer as exc:
        if exc.code != STARTING or limit <= 0:
            raise
        first = exc.answer
    if sink is None:
        from services.orchestrator.stream_client import sink_context

        sink = sink_context.get()
    started = time.monotonic()
    # The turn's deadline moves by the time waited, never more.
    async with infrastructure_startup(limit + 30):
        try:
            async for answer in waiting(first, limit):
                waited = time.monotonic() - started
                if sink is not None:
                    await sink(
                        {
                            "phase": "owned_server_starting",
                            "waited_seconds": round(waited, 1),
                            "expected_seconds": round(
                                waited + (answer.retry_after or EXPECTED_SECONDS)
                            ),
                        }
                    )
        except OwnedServerUnavailable as refused:
            raise OwnedServerAnswer(refused) from None
    if sink is not None:
        await sink({"phase": "owned_server_ready"})
    return await attempt()
