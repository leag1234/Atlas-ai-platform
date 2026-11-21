"""Answers about an on-demand owned inference server, shared by the gateway and the edge.

An owned server (``"privacy": "owned"`` preset, ``ATLAS_OWNED_VLLM_BASE_URL``) may be a
rented GPU that is powered off between uses. When a strict request finds it silent, the
gateway asks the host's starter to power it on (``owned_start``) and the request gets one
of these answers instead of a provider error:

- ``owned_server_starting`` (503, ``Retry-After``): a start is under way; nothing was sent
  anywhere else. A chat turn and an API request wait for the server instead
  (``services/orchestrator/owned_wait.py``) and answer once it is up; this answer reaches
  a client that asked not to wait, or one still waiting after the wait limit, then with
  ``waited`` (the seconds it waited);
- ``owned_server_budget`` (429): the starter refused because a budget cap is reached
  (daily hours or monthly euros), with the measured value and the limit (D5: a budget
  refusal is never reported as an outage);
- ``owned_server_start_limit`` (429, ``Retry-After``): too many starts in the last hour;
- ``owned_server_unavailable`` (503, ``Retry-After``): the start was tried and failed, with
  its ``reason``: ``capacity`` (the hosting provider has no machine of that type free right
  now) or ``start_failed`` (any other failure); or no start could be asked:
  ``starter_unavailable`` (the starter is configured but does not answer). Never reported
  as "starting" (D5: the user reads the real outcome, staging 2026-11-17: "starting" was
  shown after the start failed), nor as a provider failure (staging 2026-11-20: with the
  starter down, a strict request read "Explicit stop: provider_error").

The gateway marks these answers with the ``X-Atlas-Owned-Server`` response header (value:
the code) so that its clients name them without guessing from the status.
"""

from typing import Any

STARTING = "owned_server_starting"
BUDGET = "owned_server_budget"
START_LIMIT = "owned_server_start_limit"
UNAVAILABLE = "owned_server_unavailable"
CODES = frozenset({STARTING, BUDGET, START_LIMIT, UNAVAILABLE})
STATUS = {STARTING: 503, BUDGET: 429, START_LIMIT: 429, UNAVAILABLE: 503}
# Why a start failed: no machine of that type free at the hosting provider, or else;
# or why none could be asked: the starter does not answer.
REASONS = frozenset({"capacity", "start_failed", "starter_unavailable"})
HEADER = "X-Atlas-Owned-Server"
# Units of a measured cap: hours used today, euros spent this month, starts this hour.
UNITS = frozenset({"hours", "EUR", "starts"})
PERIODS = frozenset({"today", "month", "hour"})


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or not 0 <= value <= 10**7:
        return None
    return float(value)


def _show(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def wait_text(seconds: int) -> str:
    """A rounded-up wait for people: minutes below an hour, else hours."""
    if seconds < 3600:
        return f"{max(1, -(-seconds // 60))} min"
    return f"{-(-seconds // 3600)} h"


class OwnedServerUnavailable(RuntimeError):
    """The owned server is starting, or its starter refused to start it (named, D5)."""

    def __init__(
        self,
        code: str,
        *,
        retry_after: int | None = None,
        measured: float | None = None,
        limit: float | None = None,
        unit: str | None = None,
        period: str | None = None,
        reason: str | None = None,
        waited: int | None = None,
    ) -> None:
        if code not in CODES:
            raise ValueError("invalid_owned_server_code")
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.measured, self.limit = measured, limit
        self.unit = unit if unit in UNITS else None
        self.period = period if period in PERIODS else None
        self.reason = (
            (reason if reason in REASONS else "start_failed")
            if code == UNAVAILABLE
            else None
        )
        # Seconds the request already waited for a start (only for "starting").
        self.waited = waited if code == STARTING and waited is not None else None

    @property
    def status(self) -> int:
        return STATUS[self.code]

    def fields(self) -> dict[str, Any]:
        """Public fields: the code and the measured values, never any content."""
        values: dict[str, Any] = {
            "retry_after": self.retry_after,
            "measured": self.measured,
            "limit": self.limit,
            "unit": self.unit,
            "period": self.period,
            "reason": self.reason,
            "waited": self.waited,
        }
        return {"code": self.code} | {k: v for k, v in values.items() if v is not None}

    @property
    def message(self) -> str:
        wait = (
            f" Retry in about {wait_text(self.retry_after)}."
            if self.retry_after
            else ""
        )
        if self.code == STARTING and self.waited is not None:
            return (
                "The owned inference server for strict privacy is still starting after "
                f"a wait of {wait_text(self.waited)}. Nothing was sent to any other "
                "provider." + wait
            )
        if self.code == STARTING:
            return (
                "The owned inference server for strict privacy is starting. Nothing "
                "was sent to any other provider." + wait
            )
        if self.code == UNAVAILABLE:
            why = {
                "capacity": "the hosting provider has no machine of its type available "
                "right now",
                "starter_unavailable": "its starter does not answer",
            }.get(str(self.reason), "its start failed")
            return (
                f"The owned inference server for strict privacy cannot start: {why}. "
                "Nothing was sent to any other provider." + wait
            )
        used = (
            f": {_show(self.measured)} {self.unit} used of {_show(self.limit)} "
            f"{self.unit}" + (f" ({self.period})" if self.period else "")
            if self.measured is not None
            and self.limit is not None
            and self.unit is not None
            else ""
        )
        if self.code == START_LIMIT:
            return (
                "The owned inference server for strict privacy cannot be started again "
                f"yet{used}. Nothing was sent to any other provider." + wait
            )
        return (
            "The owned inference server for strict privacy is not started: its budget "
            f"cap is reached{used}. Nothing was sent to any other provider." + wait
        )


def parse(value: object) -> OwnedServerUnavailable | None:
    """The answer carried by a gateway response or stream event, else None."""
    if not isinstance(value, dict) or value.get("code") not in CODES:
        return None
    retry = _number(value.get("retry_after"))
    unit, period = value.get("unit"), value.get("period")
    reason = value.get("reason")
    waited = _number(value.get("waited"))
    return OwnedServerUnavailable(
        str(value["code"]),
        retry_after=int(retry) if retry is not None else None,
        measured=_number(value.get("measured")),
        limit=_number(value.get("limit")),
        unit=unit if isinstance(unit, str) else None,
        period=period if isinstance(period, str) else None,
        reason=reason if isinstance(reason, str) else None,
        waited=int(waited) if waited is not None else None,
    )
