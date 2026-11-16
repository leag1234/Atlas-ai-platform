"""Bounded SearXNG engine policy. State is shared by one serving event loop."""

import asyncio
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import importlib
import logging
import os
import re
import time
from weakref import WeakKeyDictionary
from typing import cast

import aiohttp

from packages.images import VisionMessage
from packages.language import user_prose
from services.orchestrator import search_freshness
from services.orchestrator.cache import Cache
from services.orchestrator.loop import Message
from services.orchestrator.web import MAX_BYTES

# Default order, measured from the server's IP on 2026-11-14 (40 queries per
# engine): Startpage and DuckDuckGo answered 40 and 39 with relevant pages,
# Mojeek (independent index) 32, Seznam 40 but with a Czech bias;
# Qwant, Brave and Google refused every request and stay last so that they
# rejoin when their block lifts. Bing answered every request, but about half
# of its pages were unrelated to the query, so it is accepted when configured
# and is not in the default order.
ENGINES = ("startpage", "duckduckgo", "mojeek", "seznam", "qwant", "brave", "google")
SUPPORTED = (*ENGINES, "bing")
# Engines of the pinned image that implement a time range. SearXNG silently
# skips any other engine when a request carries time_range, so a news search
# would read as empty instead of asking it (staging 2026-11-14).
TIME_RANGE_ENGINES = frozenset({"duckduckgo", "google", "startpage", "mojeek"})
# Local pacing decisions: the engine was not asked, so it is not a failure.
SKIPPED = frozenset({"cooldown", "quarantined"})
# A search waits for a busy engine only when the call can still start this
# long before the search deadline; observed engine answers take 0.5 to 2.5 s.
MIN_CALL_SECONDS = 2.0
# Adaptive order. An engine that refused at least half of the calls it really
# received in the last half hour (four calls or more) moves behind the other
# engines for half an hour, then rejoins its configured place with a fresh
# window. Three consecutive refusals already quarantine an engine (Circuit);
# this catches engines that refuse one call in two without ever reaching
# three in a row.
HEALTH_WINDOW_SECONDS = 1800.0
DEMOTE_MIN_CALLS = 4
DEMOTE_BLOCK_RATE = 0.5
DEMOTE_SECONDS = 1800.0
# Rolling health is written to the journal at most this often per process.
HEALTH_LOG_SECONDS = 600.0
LANGUAGES = {"en", "de", "es", "fr", "it", "nl"}
SEARCH_REGIONS = {
    "fr": "fr-FR",
    "en": "en-US",
    "de": "de-DE",
    "es": "es-ES",
    "it": "it-IT",
    "nl": "nl-NL",
}
# How long the sidecar suspends an engine after each kind of refusal: the
# SearXNG defaults, kept equal to search.suspended_times of
# infra/searxng/settings.yml (a test compares them). Atlas does not ask a
# refusing engine again before the sidecar would: short local suspensions
# (60 s until 2026-11-16) asked engines that had just blocked the server's
# address again and again, and the blocks lasted.
SUSPENDED_SECONDS = {
    "access_denied": 86400,
    "captcha": 86400,
    "too_many_requests": 3600,
    "recaptcha": 604800,
}
# The settings.yml key of each kind above.
SUSPENDED_SETTINGS = {
    "access_denied": "SearxEngineAccessDenied",
    "captcha": "SearxEngineCaptcha",
    "too_many_requests": "SearxEngineTooManyRequests",
    "recaptcha": "recaptcha_SearxEngineCaptcha",
}
OUTCOMES = {"upstream_blocked", "upstream_timeout", "empty_results", "partial_results"}
NOTICES = {
    "upstream_blocked": "Web search was blocked by upstream engines; no verified results were obtained.",
    "upstream_timeout": "Web search timed out; no verified results were obtained.",
    "empty_results": "The search engines returned no matching results. This does not establish that no information exists.",
    "partial_results": "Search returned partial results; some sources were unavailable or too few web results were found.",
}


def search_language(messages: Sequence[VisionMessage], locale: str) -> str:
    """Short follow-ups inherit history; ambiguous text uses an explicit UI locale."""
    detector = importlib.import_module("langdetect")

    language = locale.lower().replace("_", "-").split("-")[0]
    if language not in LANGUAGES:
        language = "en"
    detector.DetectorFactory.seed = 0
    for message in messages:
        if message.role != "user":
            continue
        prose = user_prose(message.text)
        if len(re.findall(r"\b[\w'-]+\b", prose)) < 5:
            continue
        try:
            scores = detector.detect_langs(prose.lower())
        except detector.lang_detect_exception.LangDetectException:
            continue
        if scores[0].prob >= 0.8 and scores[0].lang in LANGUAGES:
            language = str(scores[0].lang)
    return language


def search_region(language: str) -> str:
    """Map Atlas's language contract to a SearXNG engine locale."""
    return SEARCH_REGIONS[search_language([], language)]


@dataclass(frozen=True)
class Policy:
    engines: tuple[str, ...] = ENGINES
    quarantine: float = 900
    probe_interval: float = 60
    engine_interval: float = 3
    fallback_interval: float = 30
    minimum: int = 0  # Zero means the requested result count.
    # Logical searches per rolling minute for this process. Upstream engines
    # are protected by the per-engine spacing above; this bound only caps the
    # total. Ten per minute rejected three concurrent research turns
    # (staging 2026-11-15: searches waited past their deadline).
    searches_per_minute: int = 30

    def spacing(self, engine: str) -> float:
        # The first two configured engines are the primary tier.
        return (
            self.engine_interval
            if engine in self.engines[:2]
            else self.fallback_interval
        )

    @classmethod
    def environment(cls) -> "Policy":
        engines = tuple(
            os.getenv("ATLAS_SEARXNG_ENGINES", ",".join(ENGINES)).split(",")
        )
        engines = tuple(engine.strip() for engine in engines)
        policy = cls(
            engines=engines,
            quarantine=float(os.getenv("ATLAS_SEARXNG_QUARANTINE_SECONDS", "900")),
            probe_interval=float(os.getenv("ATLAS_SEARXNG_PROBE_SECONDS", "60")),
            engine_interval=float(os.getenv("ATLAS_SEARXNG_ENGINE_SECONDS", "3")),
            fallback_interval=float(os.getenv("ATLAS_SEARXNG_FALLBACK_SECONDS", "30")),
            minimum=int(os.getenv("ATLAS_SEARXNG_MIN_RESULTS", "0")),
            searches_per_minute=int(
                os.getenv("ATLAS_SEARXNG_SEARCHES_PER_MINUTE", "30")
            ),
        )
        if (
            not engines
            or len(set(engines)) != len(engines)
            or any(engine not in SUPPORTED for engine in engines)
            or not 900 <= policy.quarantine <= 3600
            or policy.probe_interval < 60
            or policy.engine_interval < 3
            or policy.fallback_interval < max(30, policy.engine_interval)
            or not 0 <= policy.minimum <= 5
            or not 10 <= policy.searches_per_minute <= 60
        ):
            raise ValueError("invalid_searxng_policy")
        return policy


@dataclass
class Circuit:
    blocks: int = 0
    until: float = 0
    next_call: float = 0
    quarantined: bool = False
    probes: int = 0
    last_start: float = float("-inf")
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def opens(self, now: float) -> float:
        """Earliest start a new booking can get: spacing and cooldowns."""
        return max(now, self.next_call, self.until)

    def book(self, now: float, interval: float) -> float:
        """Reserve the next start slot; later bookings queue behind it."""
        slot = self.opens(now)
        self.next_call = slot + interval
        return slot

    def record(self, outcome: str, now: float, retry: float, policy: Policy) -> None:
        if retry > 0:
            self.until = max(self.until, now + retry)
        if outcome == "upstream_blocked":
            self.blocks += 1
            self.until = max(self.until, now + max(retry, policy.probe_interval))
            if self.blocks >= 3 or self.quarantined:
                self.quarantined = True
                self.probes = 0
                self.until = max(self.until, now + policy.quarantine)
        elif self.quarantined:
            if outcome == "ok":
                self.probes += 1
                if self.probes >= 2:
                    self.quarantined = False
                    self.blocks = 0
                    self.until = 0
                    return
            else:
                self.probes = 0
            self.until = max(self.until, now + policy.probe_interval)
        else:
            self.blocks = 0


@dataclass
class Health:
    """Outcomes of the calls an engine really received, in a rolling window."""

    events: deque[tuple[float, str]] = field(default_factory=lambda: deque(maxlen=200))
    demoted_until: float = 0.0

    def demoted(self, now: float) -> bool:
        if self.demoted_until and now >= self.demoted_until:
            # The cooldown is over: the engine gets its place back and is
            # judged on new calls only.
            self.demoted_until = 0.0
            self.events.clear()
        return self.demoted_until > now

    def record(self, outcome: str, now: float) -> bool:
        """Record one asked call; True when this call demotes the engine."""
        demoted = self.demoted(now)
        while self.events and self.events[0][0] <= now - HEALTH_WINDOW_SECONDS:
            self.events.popleft()
        self.events.append((now, outcome))
        if demoted:
            return False
        blocked = sum(seen == "upstream_blocked" for _, seen in self.events)
        if len(self.events) >= DEMOTE_MIN_CALLS and blocked >= DEMOTE_BLOCK_RATE * len(
            self.events
        ):
            self.demoted_until = now + DEMOTE_SECONDS
            return True
        return False

    def counts(self, now: float) -> dict[str, object]:
        recent = [seen for at, seen in self.events if at > now - HEALTH_WINDOW_SECONDS]
        return {
            "asked": len(recent),
            "ok": recent.count("ok"),
            "blocked": recent.count("upstream_blocked"),
            "empty": recent.count("empty_results"),
            "timeout": recent.count("upstream_timeout"),
            "demoted_seconds": round(max(0.0, self.demoted_until - now)),
        }


def engine_order(
    state: "State", endpoint: str, engines: Sequence[str], now: float
) -> tuple[str, ...]:
    """Configured order with recently refusing engines moved to the end.

    No engine is ever dropped, and when every engine is demoted the
    configured order applies unchanged.
    """
    demoted = [
        engine
        for engine in engines
        if state.health.setdefault(endpoint + ":" + engine, Health()).demoted(now)
    ]
    if len(demoted) == len(engines):
        return tuple(engines)
    return (*(engine for engine in engines if engine not in demoted), *demoted)


def retry_after(value: str | None, now: float | None = None) -> float:
    if not isinstance(value, str) or not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(
                0, parsedate_to_datetime(value).timestamp() - (now or time.time())
            )
        except (TypeError, ValueError, OverflowError):
            return 0


@dataclass
class State:
    searches: deque[float] = field(default_factory=deque)
    slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(3))
    http_slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(3))
    circuits: dict[str, Circuit] = field(default_factory=dict)
    flights: dict[str, asyncio.Task[Message]] = field(default_factory=dict)
    health: dict[str, Health] = field(default_factory=dict)
    health_logged: float = float("-inf")

    async def admit(self, limit: int = 30, deadline: float = float("inf")) -> None:
        while True:
            now = time.monotonic()
            while self.searches and self.searches[0] <= now - 60:
                self.searches.popleft()
            if len(self.searches) < limit:
                self.searches.append(now)
                return
            wait = max(0.001, self.searches[0] + 60 - now)
            if now + wait > deadline - MIN_CALL_SECONDS:
                # Waiting cannot end in time: fail now and leave the time to
                # the next provider of the chain.
                raise TimeoutError
            await asyncio.sleep(wait)


_STATES: WeakKeyDictionary[asyncio.AbstractEventLoop, State] = WeakKeyDictionary()


def shared_state() -> State:
    loop = asyncio.get_running_loop()
    if loop not in _STATES:
        _STATES[loop] = State()
    return _STATES[loop]


def classify(reason: str) -> str:
    reason = reason.lower()
    if any(
        word in reason
        for word in (
            "captcha",
            "403",
            "429",
            "blocked",
            "suspend",
            "forbidden",
            "access denied",
            "too many",
        )
    ):
        return "upstream_blocked"
    return "upstream_timeout"


def refusal_kind(reason: str, engine: str) -> str:
    """The sidecar's refusal kind named by an unresponsive-engine reason.

    SearXNG reports "CAPTCHA", "access denied" or "too many requests", with
    "Suspended: " in front while the engine is suspended. Google's CAPTCHA is
    a reCAPTCHA. Any other failure (timeouts, parsing) has no long suspension.
    """
    text = reason.lower()
    if "captcha" in text:
        return "recaptcha" if engine == "google" else "captcha"
    if "access denied" in text or "forbidden" in text or "403" in text:
        return "access_denied"
    if "too many" in text or "429" in text:
        return "too_many_requests"
    return ""


async def engine_search(
    state: State,
    endpoint: str,
    engine: str,
    params: dict[str, str],
    policy: Policy,
    deadline: float,
    slot: float | None = None,
) -> Message:
    """Ask one engine. Without a booked slot, a busy engine is skipped."""
    from packages.privacy import require_shared

    # The query is forwarded to public search engines: not in strict privacy.
    require_shared("web_search")
    circuit = state.circuits.setdefault(endpoint + ":" + engine, Circuit())
    evidence: Message = {"engine": engine, "outcome": "upstream_timeout", "results": []}
    interval = policy.spacing(engine)
    try:
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            now = time.monotonic()
            if slot is None:
                if circuit.opens(now) > now:
                    return skipped(engine, circuit, now)
                slot = circuit.book(now, interval)
            if slot > now:
                await asyncio.sleep(slot - now)
            async with circuit.lock:
                now = time.monotonic()
                if now < circuit.until:
                    # Blocked or quarantined by a call that ended after the
                    # booking: the engine is not asked.
                    return skipped(engine, circuit, now)
                # A call that ran past its slot still leaves the full spacing
                # before the next call starts (the lock keeps booking order).
                if circuit.last_start + interval > now:
                    await asyncio.sleep(circuit.last_start + interval - now)
                circuit.last_start = time.monotonic()
                circuit.next_call = max(
                    circuit.next_call, circuit.last_start + interval
                )
                retry = 0.0
                async with state.http_slots:
                    try:
                        async with aiohttp.ClientSession(
                            timeout=aiohttp.ClientTimeout(
                                total=min(8, max(0.001, deadline - time.monotonic()))
                            ),
                            trust_env=False,
                        ) as session:
                            async with session.get(
                                endpoint + "/search",
                                params={**params, "engines": engine},
                                allow_redirects=False,
                            ) as response:
                                retry = retry_after(response.headers.get("Retry-After"))
                                evidence["http_status"] = response.status
                                if response.status in {403, 429}:
                                    evidence["outcome"] = "upstream_blocked"
                                elif response.status != 200:
                                    evidence["reason"] = "http_error"
                                else:
                                    body = bytearray()
                                    async for piece in response.content.iter_chunked(
                                        16384
                                    ):
                                        body.extend(piece)
                                        if len(body) > MAX_BYTES:
                                            raise ValueError("response_too_large")
                                    try:
                                        data = json.loads(body)
                                    except ValueError:
                                        evidence["outcome"] = classify(
                                            body[:65536].decode(errors="replace")
                                        )
                                    else:
                                        evidence.update(parse_results(data, engine))
                    except (aiohttp.ClientError, TimeoutError, ValueError):
                        evidence["reason"] = "transport_or_invalid_response"
                # The engine refused: Atlas waits as long as the sidecar
                # suspends it, so it never asks a suspended engine again.
                kind = str(evidence.get("refusal", ""))
                if kind:
                    retry = max(retry, float(SUSPENDED_SECONDS[kind]))
                circuit.record(
                    str(evidence["outcome"]), time.monotonic(), retry, policy
                )
                health = state.health.setdefault(endpoint + ":" + engine, Health())
                if health.record(str(evidence["outcome"]), time.monotonic()):
                    logging.getLogger(__name__).warning(
                        "searxng_engine_demoted %s",
                        json.dumps(
                            {
                                "engine": engine,
                                **health.counts(time.monotonic()),
                            }
                        ),
                    )
                evidence["retry_after"] = max(0, circuit.until - time.monotonic())
    except TimeoutError:
        evidence["reason"] = "deadline"
    return evidence


def skipped(engine: str, circuit: Circuit, now: float) -> Message:
    """Evidence of a local pacing decision: the engine was not asked."""
    return {
        "engine": engine,
        "outcome": "upstream_blocked",
        "results": [],
        "reason": "quarantined" if circuit.quarantined else "cooldown",
        "retry_after": circuit.opens(now) - now,
    }


def parse_results(data: object, engine: str) -> Message:
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return {"outcome": "upstream_timeout", "reason": "invalid_response"}
    items: list[Message] = []
    for row in data["results"]:
        if (
            isinstance(row, dict)
            and row.get("engine")
            and row["engine"] != engine
            and (
                not isinstance(row.get("engines"), list) or engine not in row["engines"]
            )
        ):
            continue
        if not isinstance(row, dict) or not isinstance(row.get("url"), str):
            continue
        if not row["url"].startswith(("https://", "http://")):
            continue
        items.append(
            {
                "title": str(row.get("title", ""))[:300],
                "link": row["url"][:4096],
                "snippet": str(row.get("content", ""))[:300],
                "date": str(row.get("publishedDate", ""))[:100],
                "engine": engine,
            }
        )
    failures = data.get("unresponsive_engines", [])
    reason = ""
    if isinstance(failures, list):
        for failure in failures:
            if isinstance(failure, list) and failure and failure[0] == engine:
                reason = " ".join(str(part) for part in failure[1:])
            elif failure == engine:
                reason = "unresponsive"
    outcome = classify(reason) if reason else "ok" if items else "empty_results"
    parsed: Message = {"results": items, "outcome": outcome}
    if outcome == "upstream_blocked" and (kind := refusal_kind(reason, engine)):
        # engine_search suspends the engine locally for this kind.
        parsed["refusal"] = kind
    return parsed


async def search(
    cache: Cache,
    endpoint: str,
    query: str,
    language: str,
    n: int,
    news: bool,
    timeout: float,
    admit: Callable[[], None] | None = None,
) -> Message:
    """One search. ``admit`` runs only when this call starts upstream work (a
    cache miss that joins no search in flight); it may refuse by raising."""
    policy = Policy.environment()
    language = search_language([], language)
    # Near-identical queries of one language (case, accents, punctuation,
    # stop words, word order) share the cache entry and the flight below.
    identity = search_freshness.normalized_query(query, language)
    bound = search_freshness.time_bound(query, news)
    # SearXNG query operators can override explicit engine/locale parameters.
    # Quote these tokens so untrusted search text cannot bypass the policy.
    query = re.sub(
        r"(\S+)",
        lambda match: '"' + match[0] + '"' if match[0][0] in "!:<" else match[0],
        query,
    )
    endpoint = endpoint.rstrip("/")
    key = cache.key(
        [
            "searxng-individual-v4",
            endpoint,
            identity,
            language,
            n,
            news,
            policy.__dict__,
        ]
    )
    cached = cache.get(key)
    log = logging.getLogger(__name__)
    if cached is not None:
        # Metadata only, never the query: read by tools/search_health_report.py.
        log.warning(
            "searxng_cache %s", json.dumps({"result": "hit", "time_bound": bound})
        )
        return cached
    state = shared_state()
    # Share ongoing work across Runtime instances. A cancelled waiter must not
    # cancel another user's search. The flight has its own bounded deadline.
    if key not in state.flights:
        if admit is not None:
            admit()
        task = asyncio.create_task(
            run_search(
                state,
                cache,
                key,
                endpoint,
                query,
                language,
                n,
                news,
                timeout,
                policy,
                bound,
            )
        )
        state.flights[key] = task
        task.add_done_callback(lambda done: state.flights.pop(key, None))
    else:
        log.warning(
            "searxng_cache %s", json.dumps({"result": "shared", "time_bound": bound})
        )
    try:
        async with asyncio.timeout(max(0.001, timeout - 0.02)):
            return await asyncio.shield(state.flights[key])
    except TimeoutError:
        return {
            "provider": "searxng",
            "language": language,
            "outcome": "upstream_timeout",
            "results": [],
            "engines": [],
            "message": NOTICES["upstream_timeout"],
            "consulted_at": datetime.now(timezone.utc).isoformat(),
        }


async def run_search(
    state: State,
    cache: Cache,
    key: str,
    endpoint: str,
    query: str,
    language: str,
    n: int,
    news: bool,
    timeout: float,
    policy: Policy,
    bound: bool = True,
) -> Message:
    deadline = time.monotonic() + max(0.001, timeout - 0.05)
    evidence: list[Message] = []
    # Never send "categories" with "engines": SearXNG then adds every enabled
    # engine of the category to the explicit one, so each Atlas call reached
    # all web engines at once and got this server's IP blocked (staging
    # 2026-11-14: about six upstream requests per engine call).
    params = {
        "q": query,
        "format": "json",
        "language": search_region(language),
        "safesearch": "1",
    }
    if news:
        params["time_range"] = "month"
    # The SearXNG "wikipedia" engine is not asked: it looks up an exact
    # article title and returned no result for every model query measured
    # (staging 2026-11-15: 39 of 39 calls empty), one wasted call per search.
    waited = 0.0
    order: tuple[str, ...] = policy.engines
    try:
        async with asyncio.timeout_at(deadline):
            async with state.slots:
                await state.admit(policy.searches_per_minute, deadline)
                web_rows: dict[str, Message] = {}
                # Tiers follow the adaptive order; spacing stays with the
                # configured tiers, so a promoted fallback engine is never
                # called more often than configured.
                order = engine_order(state, endpoint, policy.engines, time.monotonic())
                pending = list(order)
                while pending:
                    # Concurrent searches queue for an engine's next spacing
                    # slot instead of skipping it: a search that found every
                    # engine cooling down used to fail at once and fall to
                    # exhausted paid providers (staging 2026-11-15, three
                    # research turns). A fallback engine is taken only when it
                    # starts at least one primary spacing sooner, so the load
                    # spreads across engines without leaving the primaries.
                    now = time.monotonic()
                    circuits = {
                        engine: state.circuits.setdefault(
                            endpoint + ":" + engine, Circuit()
                        )
                        for engine in pending
                    }
                    usable = {
                        engine: circuit.opens(now)
                        + (policy.engine_interval if order.index(engine) >= 2 else 0)
                        for engine, circuit in circuits.items()
                        # A free engine is always asked; a wait must leave
                        # the call time to answer before the deadline.
                        if circuit.opens(now) <= max(now, deadline - MIN_CALL_SECONDS)
                    }
                    if not usable:
                        evidence.extend(
                            skipped(engine, circuit, now)
                            for engine, circuit in circuits.items()
                        )
                        break
                    engine = min(usable, key=lambda name: usable[name])
                    index = order.index(engine)
                    pending.remove(engine)
                    slot = circuits[engine].book(now, policy.spacing(engine))
                    waited += slot - now
                    result = await engine_search(
                        state,
                        endpoint,
                        engine,
                        params
                        if engine in TIME_RANGE_ENGINES
                        else {k: v for k, v in params.items() if k != "time_range"},
                        policy,
                        deadline,
                        slot,
                    )
                    evidence.append(result)
                    rows = result.get("results", [])
                    if isinstance(rows, list):
                        web_rows.update(
                            {
                                str(row["link"]): row
                                for row in rows
                                if isinstance(row, dict)
                            }
                        )
                    # First two tiers may accumulate sparse results. A tier-three
                    # engine is used only when neither primary produced enough.
                    if len(web_rows) >= (policy.minimum or n) or (index >= 2 and rows):
                        break
    except TimeoutError:
        evidence.append(
            {"engine": "scheduler", "outcome": "upstream_timeout", "results": []}
        )
    results: dict[str, Message] = {}
    for attempt in evidence:
        rows = attempt.get("results", [])
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    results.setdefault(str(row["link"]), row)
    outcomes = {str(attempt["outcome"]) for attempt in evidence}
    # An engine skipped for local pacing was not asked: when another engine
    # answered, the skip does not make the results partial.
    asked = {
        str(attempt["outcome"])
        for attempt in evidence
        if attempt.get("reason") not in SKIPPED
    }
    web_count = len(results)
    outcome = (
        "partial_results"
        if results
        and (asked - {"ok", "empty_results"} or web_count < (policy.minimum or n))
        else "ok"
        if results
        else "upstream_blocked"
        if "upstream_blocked" in outcomes
        else "upstream_timeout"
        if "upstream_timeout" in outcomes
        else "empty_results"
    )
    web = list(results.values())[:n]
    output: Message = {
        "provider": "searxng",
        "language": language,
        "outcome": outcome,
        "results": web,
        "engines": [
            {
                **{k: v for k, v in row.items() if k != "results"},
                "result_count": len(cast(list[object], row.get("results", []))),
            }
            for row in evidence
        ],
        "consulted_at": datetime.now(timezone.utc).isoformat(),
    }
    if outcome in NOTICES:
        output["message"] = NOTICES[outcome]
    # A result is degraded when an engine failed or was skipped: a later call
    # may get that engine. A short result with every engine answering is the
    # engines' answer and is kept as long as a full one.
    ttl = search_freshness.cache_ttl(
        bound=bound,
        degraded=outcome != "ok" and bool(outcomes - {"ok", "empty_results"}),
    )
    log = logging.getLogger(__name__)
    # Metadata only (engines, outcomes, pacing), never the query. Warning level
    # so that the serving journal shows it: the info line was not recorded and
    # a search failure under concurrency could not be read (2026-11-15).
    log.warning(
        "searxng_outcome %s",
        json.dumps(
            {
                **{
                    k: v
                    for k, v in output.items()
                    if k not in {"results", "consulted_at", "message"}
                },
                "waited_seconds": round(waited, 2),
                "order": list(order) if order != policy.engines else None,
                "time_bound": bound,
                "cache_ttl": ttl if results else 0,
            }
        ),
    )
    now = time.monotonic()
    if now - state.health_logged >= HEALTH_LOG_SECONDS:
        state.health_logged = now
        log.warning(
            "searxng_health %s",
            json.dumps(
                {
                    "window_seconds": HEALTH_WINDOW_SECONDS,
                    "engines": {
                        engine: state.health.setdefault(
                            endpoint + ":" + engine, Health()
                        ).counts(now)
                        for engine in policy.engines
                    },
                }
            ),
        )
    if results:
        cache.put(key, output, ttl)
    return output
