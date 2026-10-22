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
