"""Who may spend upstream web searches, and how many.

Real users and test traffic share the same upstream engines and paid quotas.
On 2026-11-16 test agents searching through staging got the server's address
blocked by the free engines while the paid allowances were used up, and real
users had no web search. This module decides, by code and configuration only:

- whether a request comes from a test identity (``ATLAS_SEARCH_TEST_IDENTITIES``,
  a comma list of exact identities, and ``ATLAS_SEARCH_TEST_PREFIXES``, a comma
  list of prefixes; matched against the account id and email, the API key id,
  subject, budget bucket and public key prefix). Test traffic has its own small daily cap and, by default, never
  reaches a metered provider;
- the per-user and per-conversation daily caps, so one person or a runaway
  loop cannot use the engines up for everyone;
- whether a search is answered from a recorded replay (``ATLAS_SEARCH_REPLAY``)
  instead of upstream, so that live evaluations do not spend upstream calls.

Only searches that reach upstream count: cache hits, searches that join an
identical search in flight and replays are free. A refused search is reported
as a limit, never as an unavailable search (D5).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path

from services.orchestrator import search_freshness
from services.orchestrator.daily_budget import BudgetRefusal, DailyLedger
from services.orchestrator.loop import Message

# Daily upstream searches. Zero disables a cap. A research turn runs up to
# about ten searches; production traffic was a few dozen to a few hundred
# searches a day in all (survey 2026-11-16).
DEFAULT_USER_DAILY = 150
DEFAULT_CONVERSATION_DAILY = 60
DEFAULT_TEST_DAILY = 40
# Providers billed per query: their allowance belongs to real users.
METERED = frozenset({"serpapi", "tavily"})
PROVIDERS = frozenset({"serpapi", "tavily", "searxng"})
TEST_BUCKET = "test-identities"


class SearchCapRefusal(BudgetRefusal):
    """A cap of this person, conversation or test traffic: no provider is asked."""


@dataclass(frozen=True)
class Scope:
    user: str = "unknown"
    conversation: str = ""
    test: bool = False


def _cap(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, value)


def _listed(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(part.strip().casefold() for part in raw.split(",") if part.strip())


def is_test(identities: Iterable[str]) -> bool:
    """Whether any identity of the request is a configured test identity:
    equal to an entry of ATLAS_SEARCH_TEST_IDENTITIES or starting with an
    entry of ATLAS_SEARCH_TEST_PREFIXES (case-insensitive)."""
    exact, prefixes = (
        _listed("ATLAS_SEARCH_TEST_IDENTITIES"),
        _listed("ATLAS_SEARCH_TEST_PREFIXES"),
    )
    for identity in identities:
        value = str(identity).strip().casefold()
        if not value or value == "unknown":
            continue
        if value in exact or any(value.startswith(prefix) for prefix in prefixes):
            return True
    return False


def scope(user: str, conversation: str, identities: Sequence[str]) -> Scope:
    return Scope(
        user=user or "unknown",
        conversation=conversation,
        test=is_test((user, *identities)),
    )


def test_providers(chain: Sequence[str]) -> tuple[str, ...]:
    """Provider chain of test traffic: by default the configured one without
    metered providers (or unchanged when only metered providers are set)."""
    raw = os.environ.get("ATLAS_SEARCH_TEST_PROVIDERS", "").strip()
    if raw:
        chosen = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
        if not chosen or any(provider not in PROVIDERS for provider in chosen):
            raise ValueError("invalid_search_provider")
        return chosen
    return tuple(provider for provider in chain if provider not in METERED) or tuple(
        chain
    )


def admit(ledger: DailyLedger | None, scope: Scope, lang: str) -> None:
    """Count one upstream search against the caps of this scope, or refuse it."""
    if ledger is None:
        return
    checks: list[tuple[str, str, str, int]] = []
    if scope.test:
        cap = _cap("ATLAS_SEARCH_TEST_DAILY", DEFAULT_TEST_DAILY)
        if cap:
            checks.append(("test_daily_search", "search_test_day", TEST_BUCKET, cap))
    elif scope.user != "unknown":
        cap = _cap("ATLAS_SEARCH_USER_DAILY", DEFAULT_USER_DAILY)
        if cap:
            checks.append(("daily_search", "search_user_day", scope.user, cap))
    if scope.conversation:
        cap = _cap("ATLAS_SEARCH_CONVERSATION_DAILY", DEFAULT_CONVERSATION_DAILY)
        if cap:
            checks.append(
                (
                    "conversation_daily_search",
                    "search_conversation_day",
                    "conversation:" + scope.conversation,
                    cap,
                )
            )
    ledger.reserve_counted_searches(checks, lang, SearchCapRefusal)


def refusal_text(exc: BudgetRefusal) -> str:
    """Model-visible wording of a search limit: always a limit (D5)."""
    period = "this month" if "monthly" in exc.category else "today"
    whose = (
        "for this conversation "
        if exc.category.startswith("conversation_")
        else "for test traffic "
        if exc.category.startswith("test_")
        else ""
    )
    return f"search limit reached {whose}{period} ({exc.used} of {exc.limit})"


# Recorded replays: one JSON object per line,
# {"query": ..., "lang": ..., "news": false, "result": {...}}.


def replay_key(query: str, lang: str, news: bool) -> str:
    return json.dumps([search_freshness.normalized_query(query, lang), lang, news])


@dataclass(frozen=True)
class Replay:
    entries: dict[str, Message]
    live_on_miss: bool

    def lookup(self, query: str, lang: str, news: bool) -> Message | None:
        found = self.entries.get(replay_key(query, lang, news))
        return None if found is None else {**found, "replayed": True}


_LOADED: dict[tuple[str, float], dict[str, Message]] = {}


def replay(test: bool) -> Replay | None:
    """The replay that answers this request's searches, if one is configured."""
    path = os.environ.get("ATLAS_SEARCH_REPLAY", "").strip()
    if not path:
        return None
    applies = os.environ.get("ATLAS_SEARCH_REPLAY_SCOPE", "test").strip() or "test"
    if applies not in {"test", "all"}:
        raise ValueError("invalid_search_replay")
    if applies == "test" and not test:
        return None
    source = Path(path)
    try:
        stamp = (str(source), source.stat().st_mtime)
    except OSError as exc:
        raise ValueError("invalid_search_replay") from exc
    if stamp not in _LOADED:
        entries: dict[str, Message] = {}
        for line in source.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("query"), str)
                or not isinstance(row.get("lang"), str)
                or not isinstance(row.get("result"), dict)
            ):
                raise ValueError("invalid_search_replay")
            entries[replay_key(row["query"], row["lang"], row.get("news") is True)] = (
                row["result"]
            )
        _LOADED.clear()
        _LOADED[stamp] = entries
    return Replay(
        _LOADED[stamp],
        os.environ.get("ATLAS_SEARCH_REPLAY_MISS", "refuse").strip() == "live",
    )


def record(query: str, lang: str, news: bool, result: Message) -> None:
    """Append a live test search to ATLAS_SEARCH_RECORD (test traffic only:
    real users' queries are never written)."""
    path = os.environ.get("ATLAS_SEARCH_RECORD", "").strip()
    if not path or not result.get("results"):
        return
    kept = {k: v for k, v in result.items() if k not in {"attempts", "replayed"}}
    line = json.dumps(
        {"query": query, "lang": lang, "news": news, "result": kept},
        ensure_ascii=False,
    )
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
