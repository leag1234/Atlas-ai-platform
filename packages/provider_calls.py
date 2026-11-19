"""Per-request record of the calls that can reach a model provider.

API key billing keeps the full reservation as the charge when a request ends while a
provider call may have consumed tokens that were never reported (a call cut in flight,
a transport failure). Every egress that can reach a model provider (the model gateway
and the retrieval service) marks the current request before it sends, then records how
the call came back: answered with its measured cost, or refused by the gateway before
any provider output. A request whose every call came back in one of these two ways
knows what it consumed, so a refusal (budget, quota, provider unavailable) or a failure
before the answer is charged that measured cost, never the reservation held for the
answer it did not produce. A request refused before any such call consumed nothing.
"""

from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


@dataclass
class ProviderCalls:
    started: bool = False
    # Calls sent whose outcome never came back: their cost is unknown.
    pending: int = 0
    # Cost the calls reported when they came back, plus costs priced outside the
    # gateway (OCR pages): what the request consumed so far.
    measured: Decimal = field(default_factory=lambda: Decimal(0))

    @property
    def unmeasured(self) -> bool:
        """True while a sent call may have consumed tokens nobody measured."""
        return self.pending > 0

    def answered(self, cost: object = None) -> None:
        """One sent call came back: with its reported cost, or refused (no cost)."""
        self.pending = max(0, self.pending - 1)
        self.add(cost)

    def add(self, cost: object) -> None:
        """Add a cost measured for this request (ignored when absent or invalid)."""
        if cost is None or isinstance(cost, bool):
            return
        try:
            value = Decimal(str(cost))
        except (InvalidOperation, ValueError):
            return
        if value.is_finite() and value > 0:
            self.measured += value


_current: ContextVar[ProviderCalls | None] = ContextVar("provider_calls", default=None)


def track() -> ProviderCalls:
    """Start a fresh record for the current request; child tasks share it."""
    return use(ProviderCalls())


def use(calls: ProviderCalls) -> ProviderCalls:
    """Bind an existing record in the current context (for example a stream task)."""
    _current.set(calls)
    return calls


def mark_started() -> ProviderCalls | None:
    """Record one call about to be sent; returns the record to settle it on."""
    calls = _current.get()
    if calls is not None:
        calls.started = True
        calls.pending += 1
    return calls


def add_cost(cost: object) -> None:
    """Record a cost priced outside the gateway answers (for example OCR pages)."""
    calls = _current.get()
    if calls is not None:
        calls.add(cost)
