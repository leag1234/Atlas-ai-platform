"""Inference deadline with a separately bounded infrastructure startup window."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from collections.abc import AsyncIterator
import time

_current: ContextVar[asyncio.Timeout | None] = ContextVar(
    "request_deadline", default=None
)


@asynccontextmanager
async def request_deadline(seconds: float) -> AsyncIterator[None]:
    async with asyncio.timeout(seconds) as deadline:
        token = _current.set(deadline)
        try:
            yield
        finally:
            _current.reset(token)


def extend(seconds: float) -> None:
    """Move the current request deadline later, for a turn routed after it was set.

    The deadline is set from ChatRequest.timeout_seconds before the turn runs; a
    model routing decision taken inside the turn can make it a 300 s turn.
    """
    deadline = _current.get()
    if seconds <= 0 or deadline is None or deadline.expired():
        return
    when = deadline.when()
    if when is not None:
        deadline.reschedule(when + seconds)


@asynccontextmanager
async def infrastructure_startup(seconds: float) -> AsyncIterator[None]:
    """Only GPU readiness can use this window; inference keeps its old allowance."""
    deadline = _current.get()
    original = deadline.when() if deadline is not None else None
    started = time.monotonic()
    if deadline is not None and original is not None:
        deadline.reschedule(original + seconds)
    try:
        async with asyncio.timeout(seconds):
            yield
    finally:
        if deadline is not None and original is not None and not deadline.expired():
            deadline.reschedule(original + min(seconds, time.monotonic() - started))
