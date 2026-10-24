"""Request-scoped cancellation for browser streams.

The browser receives an unguessable identifier with the Open WebUI request body.
Only the task that owns that identifier can be cancelled; registrations disappear
when the SSE producer finishes so a later turn cannot affect an old one.
"""

import asyncio


_running: dict[str, asyncio.Task[None]] = {}
_user_cancelled: set[str] = set()


def register(token: str, task: asyncio.Task[None]) -> None:
    if token in _running:
        raise ValueError("duplicate_cancel_token")
    _running[token] = task


def unregister(token: str) -> None:
    _running.pop(token, None)
    _user_cancelled.discard(token)


def cancel(token: str) -> bool:
    task = _running.get(token)
    if task is None or task.done():
        return False
    _user_cancelled.add(token)
    task.cancel("cancelled_by_user")
    return True


def cancelled_by_user(token: str | None) -> bool:
    return token is not None and token in _user_cancelled


def active_count() -> int:
    return len(_running)
