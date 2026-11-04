"""Per-request marker of the first call that can reach a model provider.

API key billing keeps the full reservation as the charge when a request ends without
measured usage, because a provider may have consumed tokens that were never reported.
A request refused before any such call (memory store unreachable, deploy switch, ...)
consumed nothing: this marker proves that case. Every egress that can reach a model
provider (the model gateway and the retrieval service) marks the current request
before it sends, so a call aborted mid-flight is still charged conservatively.
"""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class ProviderCalls:
    started: bool = False


_current: ContextVar[ProviderCalls | None] = ContextVar("provider_calls", default=None)


def track() -> ProviderCalls:
    """Start a fresh marker for the current request; child tasks share it."""
    return use(ProviderCalls())


def use(calls: ProviderCalls) -> ProviderCalls:
    """Bind an existing marker in the current context (for example a stream task)."""
    _current.set(calls)
    return calls


def mark_started() -> None:
    calls = _current.get()
    if calls is not None:
        calls.started = True
