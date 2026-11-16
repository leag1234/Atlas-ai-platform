"""A position shared in a conversation answers its follow-ups for a bounded time.

Owner report 2026-11-16 (staging): a trip question asked the device for its
position, then the correction sent two minutes later in the same conversation
asked for it again. The consent dialog's "this time only" choice now covers the
conversation it was given in, for at most ATLAS_LOCATION_REUSE_MINUTES (default
60, never more; 0 turns reuse off): a follow-up that needs the position reuses
it instead of asking again.

Nothing is written anywhere: the entry lives in this process's memory, keyed by
a digest of the person and the conversation, and is forgotten when it expires,
when the process restarts, or when the person refuses or shares another place.
"Always share" keeps working as before (the browser remembers the permission,
never the position). Refusals and unusable fixes are not kept: the next turn
may ask again. One adapter worker serves a deployment (location.LocationTickets
states the same constraint).
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import TYPE_CHECKING

from services.orchestrator.location import Location

if TYPE_CHECKING:
    from services.orchestrator.chat_schema import ChatRequest

MAX_MINUTES = 60
DEFAULT_MINUTES = 60
CAPACITY = 1024


def reuse_seconds() -> float:
    try:
        minutes = float(
            os.environ.get("ATLAS_LOCATION_REUSE_MINUTES", str(DEFAULT_MINUTES))
        )
    except ValueError:
        minutes = DEFAULT_MINUTES
    return max(0.0, min(float(MAX_MINUTES), minutes)) * 60


def conversation_key(request: ChatRequest) -> str | None:
    """Who and which conversation, or None when either is unknown (API calls)."""
    extras = request.__dict__
    user = str(extras.get("daily_user") or request._project_subject or "")
    chat = str(extras.get("native_chat_id") or request.conversation_id or "")
    if not user or not chat or user == "unknown":
        return None
    return hashlib.sha256(f"{user}\0{chat}".encode()).hexdigest()


def usable(location: Location | None) -> bool:
    """A position or a place the person gave, precise enough to be used."""
    return (
        location is not None
        and location.consent in {"once", "always"}
        and (location.position is not None or bool(location.place))
        and not location.coarse
    )


class ConversationPositions:
    def __init__(self, clock=time.monotonic) -> None:  # type: ignore[no-untyped-def]
        self.clock = clock
        self.values: dict[str, tuple[Location, float]] = {}

    def prune(self) -> None:
        now = self.clock()
        for key in [k for k, (_, expiry) in self.values.items() if expiry <= now]:
            del self.values[key]

    def remember(self, key: str | None, location: Location) -> None:
        seconds = reuse_seconds()
        if key is None or seconds <= 0 or not usable(location):
            return
        self.prune()
        if key not in self.values and len(self.values) >= CAPACITY:
            # The oldest entry goes first; nothing is ever written to disk.
            oldest = min(self.values, key=lambda k: self.values[k][1])
            del self.values[oldest]
        self.values[key] = (location.model_copy(deep=True), self.clock() + seconds)

    def recall(self, key: str | None) -> Location | None:
        if key is None or reuse_seconds() <= 0:
            return None
        self.prune()
        entry = self.values.get(key)
        return entry[0].model_copy(deep=True) if entry else None

    def forget(self, key: str | None) -> None:
        if key is not None:
            self.values.pop(key, None)


positions = ConversationPositions()
