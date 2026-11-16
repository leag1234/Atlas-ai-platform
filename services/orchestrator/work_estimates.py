"""Measured durations and costs of Work steps, and the estimate of a plan.

Design note of 2026-11-16 (long tasks, lot 1): the model says what the steps
are and of which type; the code turns that into a duration and cost range
from measures, never from the model's own guess. One measure is the time and
cost one turn spent on one step type (type, profile, path, seconds, EUR,
success), without any request or answer content. The history is seeded from
the staging corpus runs (config/work-estimate-seed.json, built by
tools/work_estimate_seed.py) and grows with every finished plan in
ATLAS_DATA_DIR/work-estimates.sqlite.

While a step type has fewer than PRUDENT_MEASURES measures, the range is
widened and the chat shows a prudent wording instead of numbers.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

# Step types of a plan, in the order a task goes through them. "setup" is the
# preparation of the workspace before a Work turn, and "turn" a whole turn,
# from the request to the answer: measured, never shown as steps.
STEP_TYPES = ("code", "install", "test", "document", "research", "deliver")
ORDER = ("setup", "research", "install", "code", "document", "test", "deliver")
MEASURED = (*ORDER, "turn")
SEED = Path("config/work-estimate-seed.json")
PRUDENT_MEASURES = 20
# Quantiles of the announced range (design note: p50 to p90).
LOW, HIGH = 0.5, 0.9
# A type without any measure: a deliberately wide prior (seconds, EUR).
PRIOR = {
    "setup": (10.0, 35.0, 0.0, 0.0),
    "research": (20.0, 60.0, 0.003, 0.01),
    "install": (20.0, 90.0, 0.003, 0.02),
    "code": (40.0, 150.0, 0.01, 0.05),
    "document": (40.0, 150.0, 0.01, 0.05),
    "test": (20.0, 90.0, 0.005, 0.03),
    "deliver": (5.0, 25.0, 0.001, 0.01),
}
# Size classes (owner decision 2026-11-16): small under 5 min and 0.50 EUR;
# large over 60 min, 2 EUR or a quarter of the person's remaining month.
SMALL_SECONDS, LARGE_SECONDS = 300.0, 3600.0
SMALL_EUR, LARGE_EUR = Decimal("0.50"), Decimal("2")
LARGE_SHARE_OF_MONTH = Decimal("0.25")
# Measures kept per key when reading (the most recent ones).
WINDOW = 400
# A step type present in fewer turns of a path than this share adds its own
# range to the whole-turn one (research in a code turn, an install).
RARE_SHARE = 0.3
ADDITIVE = frozenset({"research", "install"})

log = logging.getLogger(__name__)


def route(kind: str | None) -> str:
    """The measured path of a Work turn: a file runs Atlas's loop, code to run
    starts the code engine (20 to 40 s more before the first step)."""
    return "work-file" if kind == "file" else "work-run"


def path() -> Path:
    return Path(os.environ.get("ATLAS_DATA_DIR", "BRAIN")) / "work-estimates.sqlite"


def _connect() -> sqlite3.Connection:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(target, timeout=5)
    db.execute(
        "CREATE TABLE IF NOT EXISTS step_measures (path TEXT NOT NULL, type TEXT NOT NULL, "
        "profile TEXT NOT NULL, seconds REAL NOT NULL, cost_eur REAL NOT NULL, "
        "success INTEGER NOT NULL, recorded REAL NOT NULL)"
    )
    target.chmod(0o600)
    return db


_seed_cache: tuple[float, list[tuple[str, str, str, float, float, bool]]] | None = None


def _seed() -> list[tuple[str, str, str, float, float, bool]]:
    global _seed_cache
    try:
        stamp = SEED.stat().st_mtime
    except OSError:
        return []
    if _seed_cache is not None and _seed_cache[0] == stamp:
        return _seed_cache[1]
    try:
        rows = json.loads(SEED.read_text())["measures"]
        parsed = [
            (str(p), str(t), str(f), float(s), float(c), bool(ok))
            for p, t, f, s, c, ok in rows
        ]
    except (OSError, ValueError, KeyError, TypeError):
        parsed = []
    _seed_cache = (stamp, parsed)
    return parsed


def record(rows: Iterable[tuple[str, str, str, float, float, bool]]) -> None:
    """Store measures (path, type, profile, seconds, EUR, success); never fails a turn."""
    values = [
        (p, t, f, round(max(0.0, s), 1), round(max(0.0, c), 6), int(ok), time.time())
        for p, t, f, s, c, ok in rows
        if t in MEASURED
    ]
    if not values:
        return
    try:
        with closing(_connect()) as db, db:
            db.executemany("INSERT INTO step_measures VALUES (?,?,?,?,?,?,?)", values)
    except (sqlite3.Error, OSError) as exc:
        log.warning("work_estimate_record_failed error=%s", type(exc).__name__)


def _live(route: str, step: str, profile: str | None) -> list[tuple[float, float]]:
    try:
        with closing(_connect()) as db:
            query = (
                "SELECT seconds, cost_eur FROM step_measures WHERE path=? AND type=? "
                "AND success=1"
                + (" AND profile=?" if profile else "")
                + " ORDER BY recorded DESC LIMIT ?"
            )
            arguments: tuple[object, ...] = (
                (route, step, profile, WINDOW) if profile else (route, step, WINDOW)
            )
            return [(float(s), float(c)) for s, c in db.execute(query, arguments)]
    except (sqlite3.Error, OSError):
        return []


def samples(route: str, step: str, profile: str) -> list[tuple[float, float]]:
    """Successful measures for this path, type and profile; those of every
    profile while this one has too few (a first live measure of a profile
    never replaces hundreds of measures of the others)."""
    found: list[tuple[float, float]] = []
    for wanted in (profile or None, None):
        found = _live(route, step, wanted) + [
            (s, c)
            for p, t, f, s, c, ok in _seed()
            if p == route and t == step and ok and (wanted is None or f == wanted)
        ]
        if len(found) >= PRUDENT_MEASURES:
            break
    return found[:WINDOW]


def quantile(values: Sequence[float], q: float) -> float:
    """Nearest-rank quantile of a non-empty sequence."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


@dataclass(frozen=True)
class Estimate:
    low_seconds: float
    high_seconds: float
    low_eur: Decimal
    high_eur: Decimal
    # The number of whole turns measured behind the range.
    measures: int

    @property
    def prudent(self) -> bool:
        return self.measures < PRUDENT_MEASURES


def _type_range(
    route: str, step: str, profile: str
) -> tuple[float, float, float, float, int]:
    found = samples(route, step, profile)
    if not found:
        return (*PRIOR.get(step, PRIOR["code"]), 0)
    durations = [s for s, _ in found]
    costs = [c for _, c in found]
    low, high = quantile(durations, LOW), quantile(durations, HIGH)
    low_cost, high_cost = quantile(costs, LOW), quantile(costs, HIGH)
    if len(found) < PRUDENT_MEASURES:
        # Few measures: a wider range on both sides.
        low, high = low * 0.6, max(high * 1.5, low * 2)
        low_cost, high_cost = low_cost * 0.6, max(high_cost * 1.5, low_cost * 2)
    return low, high, low_cost, high_cost, len(found)


def _profile_scope(route: str, profile: str) -> str | None:
    """The profile whose measures are used: this one when it has enough
    whole turns measured, else every profile."""
    if not profile:
        return None
    seeded = sum(
        1 for p, t, f, *_ in _seed() if p == route and t == "turn" and f == profile
    )
    enough = seeded + len(_live(route, "turn", profile)) >= PRUDENT_MEASURES
    return profile if enough else None


def _count(route: str, step: str, scope: str | None) -> int:
    return len(_live(route, step, scope)) + sum(
        1
        for p, t, f, _, _, ok in _seed()
        if p == route and t == step and ok and (scope is None or f == scope)
    )


def estimate(route: str, profile: str, types: Iterable[str]) -> Estimate:
    """The p50 to p90 range of a whole turn whose plan has these step types.

    Measured on whole turns of the same path (app, document, code to run) and
    profile: the time from the request to the answer. A step type that turns
    of this path seldom have (research in a code turn, an install) adds its
    own measured range; deviations add as independent ones (square root of
    the sum of squares). Without turn measures, the step types add up.
    """
    wanted = sorted(set(types) & set(ORDER), key=ORDER.index)
    scope = _profile_scope(route, profile)
    turns = samples(route, "turn", scope or "")
    if not turns:
        return _by_type(route, profile, wanted)
    durations = [s for s, _ in turns]
    costs = [c for _, c in turns]
    low, high = quantile(durations, LOW), quantile(durations, HIGH)
    low_eur, high_eur = quantile(costs, LOW), quantile(costs, HIGH)
    spread, spread_eur = [high - low], [high_eur - low_eur]
    for step in wanted:
        # Code, tests, documents and the answer are part of any turn of the
        # path, even when no status showed them; research and installs add.
        if step not in ADDITIVE or _count(route, step, scope) >= RARE_SHARE * len(
            turns
        ):
            continue
        extra = _type_range(route, step, profile)
        low, low_eur = low + extra[0], low_eur + extra[2]
        spread.append(extra[1] - extra[0])
        spread_eur.append(extra[3] - extra[2])
    high = low + math.sqrt(sum(value**2 for value in spread))
    high_eur = low_eur + math.sqrt(sum(value**2 for value in spread_eur))
    if len(turns) < PRUDENT_MEASURES:
        low, high = low * 0.6, max(high * 1.5, low * 2)
    return Estimate(
        low,
        max(high, low),
        Decimal(str(round(low_eur, 4))),
        Decimal(str(round(max(high_eur, low_eur), 4))),
        len(turns),
    )


def _by_type(route: str, profile: str, wanted: Sequence[str]) -> Estimate:
    if route.startswith("work"):
        wanted = ["setup", *wanted]
    if not wanted:
        return Estimate(0.0, 0.0, Decimal(0), Decimal(0), 0)
    ranges = [_type_range(route, step, profile) for step in wanted]
    low = sum(r[0] for r in ranges)
    high = low + math.sqrt(sum((r[1] - r[0]) ** 2 for r in ranges))
    low_eur = sum(r[2] for r in ranges)
    high_eur = low_eur + math.sqrt(sum((r[3] - r[2]) ** 2 for r in ranges))
    return Estimate(
        low,
        max(high, low),
        Decimal(str(round(low_eur, 4))),
        Decimal(str(round(max(high_eur, low_eur), 4))),
        # No measure of whole turns: always the prudent wording.
        0,
    )


def size_class(found: Estimate, remaining_month_eur: Decimal | None) -> str:
    """small, medium or large (computed and logged only: no effect yet, lot 1)."""
    if (
        found.high_seconds > LARGE_SECONDS
        or found.high_eur > LARGE_EUR
        or (
            remaining_month_eur is not None
            and found.high_eur > remaining_month_eur * LARGE_SHARE_OF_MONTH
        )
    ):
        return "large"
    if found.high_seconds < SMALL_SECONDS and found.high_eur < SMALL_EUR:
        return "small"
    return "medium"


def remaining_month(ledger: object, user: str) -> Decimal | None:
    """What is left of the person's monthly limit, or None without a limit."""
    from services.orchestrator.daily_budget import DailyLedger, _limit

    limit = _limit("ATLAS_USER_MONTHLY_EUR")
    if limit is None or not isinstance(ledger, DailyLedger) or not user:
        return None
    try:
        month = ledger.periods()[1]
        with ledger.connection() as db:
            row = db.execute(
                "SELECT reserved_micro + measured_micro FROM budget_ledger "
                "WHERE kind='month' AND period=? AND user=?",
                (month, user),
            ).fetchone()
    except (sqlite3.Error, OSError):
        return None
    used = Decimal(int(row[0]) if row else 0) / 1_000_000
    return max(Decimal(0), Decimal(limit) - used)
