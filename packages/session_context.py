"""Pure context retention and calibrated threshold decisions for project sessions."""

from __future__ import annotations

import math
import os
from typing import Any

from packages.context_usage import encoded, tokens


class Calibration:
    def __init__(self) -> None:
        self.factors: dict[str, float] = {}
        self.observed: dict[tuple[str, str], int] = {}

    def count(self, family: str, payload: str, estimate: int) -> int:
        return self.observed.get(
            (family, payload), math.ceil(estimate * self.factors.get(family, 4 / 3))
        )

    def observe(self, family: str, payload: str, estimate: int, provider: int) -> None:
        if estimate <= 0 or provider <= 0:
            return
        # A high-water correction avoids turning a favourable sample into an
        # unsafe underestimate for another language or tool serialization.
        self.factors[family] = max(self.factors.get(family, 4 / 3), provider / estimate)
        if len(self.observed) >= 128:
            self.observed.pop(next(iter(self.observed)))
        self.observed[(family, payload)] = provider


def threshold(count: int, window: int) -> str:
    if count * 100 >= window * 95:
        return "refuse"
    if count * 100 >= window * 80:
        return "warning"
    if count * 100 >= window * 60:
        return "compact"
    return "normal"


def recent(
    turns: list[dict[str, Any]], represented: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    keep = max(1, min(100, int(os.environ.get("ATLAS_CONTEXT_RECENT_TURNS", "8"))))
    budget = max(1, int(os.environ.get("ATLAS_CONTEXT_RECENT_TOKENS", "20000")))
    retained, dropped = [], []
    used = 0
    for index, turn in enumerate(reversed(turns)):
        size = tokens(encoded(turn["content_refs"]))
        if (
            turn["id"] in represented
            and index > 0
            and (index >= keep or used + size > budget)
        ):
            dropped.append(turn)
        else:
            retained.append(turn)
            used += size
    return list(reversed(retained)), list(reversed(dropped))


def transport_context(context: dict[str, Any]) -> dict[str, Any]:
    """Do not retransmit represented history that the answer would discard."""
    if "session_state" not in context:
        return context
    kept, dropped = recent(context["history"], set(context["represented_turn_ids"]))
    if not dropped:
        return context
    return dict(
        context,
        history=kept,
        history_compaction=dict(
            turn_ids=[t["id"] for t in dropped],
            tokens_removed=sum(tokens(encoded(t["content_refs"])) for t in dropped),
            tokens_added=tokens(encoded(context["session_state"])),
            reason="recent_window",
        ),
    )
