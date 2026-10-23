"""Bounded SearXNG engine policy. State is shared by one serving event loop."""

import asyncio
from collections import deque
from collections.abc import Sequence
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
from services.orchestrator.cache import Cache
from services.orchestrator.loop import Message
from services.orchestrator.web import MAX_BYTES

ENGINES = ("duckduckgo", "brave", "qwant", "startpage", "bing", "google")
LANGUAGES = {"en", "de", "es", "fr", "it", "nl"}
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


@dataclass(frozen=True)
class Policy:
    engines: tuple[str, ...] = ENGINES
    quarantine: float = 900
    probe_interval: float = 60
    engine_interval: float = 6
    fallback_interval: float = 30
    minimum: int = 0  # Zero means the requested result count.

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
            engine_interval=float(os.getenv("ATLAS_SEARXNG_ENGINE_SECONDS", "6")),
            fallback_interval=float(os.getenv("ATLAS_SEARXNG_FALLBACK_SECONDS", "30")),
            minimum=int(os.getenv("ATLAS_SEARXNG_MIN_RESULTS", "0")),
        )
        if (
            not engines
            or len(set(engines)) != len(engines)
            or any(engine not in ENGINES for engine in engines)
            or not 900 <= policy.quarantine <= 3600
            or policy.probe_interval < 60
            or policy.engine_interval < 6
            or policy.fallback_interval < max(30, policy.engine_interval)
            or not 0 <= policy.minimum <= 5
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
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

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


def retry_after(value: str | None, now: float | None = None) -> float:
    if not value:
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

    async def admit(self) -> None:
        while True:
            now = time.monotonic()
            while self.searches and self.searches[0] <= now - 60:
                self.searches.popleft()
            if len(self.searches) < 10:
                self.searches.append(now)
                return
            await asyncio.sleep(max(0.001, self.searches[0] + 60 - now))


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


async def engine_search(
    state: State,
    endpoint: str,
    engine: str,
    params: dict[str, str],
    policy: Policy,
    deadline: float,
) -> Message:
    circuit = state.circuits.setdefault(endpoint + ":" + engine, Circuit())
    evidence: Message = {"engine": engine, "outcome": "upstream_timeout", "results": []}
    try:
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            async with circuit.lock:
                now = time.monotonic()
                if now < circuit.until or now < circuit.next_call:
                    return {
                        **evidence,
                        "outcome": "upstream_blocked",
                        "reason": "quarantined" if circuit.quarantined else "cooldown",
                        "retry_after": max(circuit.until, circuit.next_call) - now,
                    }
                interval = (
                    policy.engine_interval
                    if engine in {"duckduckgo", "brave", "wikipedia"}
                    else policy.fallback_interval
                )
                circuit.next_call = now + interval
                retry = 0.0
                async with state.http_slots:
                    try:
                        async with aiohttp.ClientSession(
                            timeout=aiohttp.ClientTimeout(
                                total=min(4, max(0.001, deadline - time.monotonic()))
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
                circuit.record(
                    str(evidence["outcome"]), time.monotonic(), retry, policy
                )
                evidence["retry_after"] = max(0, circuit.until - time.monotonic())
    except TimeoutError:
        evidence["reason"] = "deadline"
    return evidence


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
    return {"results": items, "outcome": outcome}


async def search(
    cache: Cache,
    endpoint: str,
    query: str,
    language: str,
    n: int,
    news: bool,
    timeout: float,
) -> Message:
    policy = Policy.environment()
    language = search_language([], language)
    # SearXNG query operators can override explicit engine/locale parameters.
    # Quote these tokens so untrusted search text cannot bypass the policy.
    query = re.sub(
        r"(\S+)",
        lambda match: '"' + match[0] + '"' if match[0][0] in "!:<" else match[0],
        query,
    )
    endpoint = endpoint.rstrip("/")
    key = cache.key(
        ["searxng-individual", endpoint, query, language, n, news, policy.__dict__]
    )
    cached = cache.get(key)
    if cached is not None:
        return cached
    state = shared_state()
    # Share ongoing work across Runtime instances. A cancelled waiter must not
    # cancel another user's search. The flight has its own bounded deadline.
    if key not in state.flights:
        task = asyncio.create_task(
            run_search(
                state, cache, key, endpoint, query, language, n, news, timeout, policy
            )
        )
        state.flights[key] = task
        task.add_done_callback(lambda done: state.flights.pop(key, None))
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
) -> Message:
    deadline = time.monotonic() + max(0.001, timeout - 0.05)
    evidence: list[Message] = []
    params = {
        "q": query,
        "format": "json",
        "language": language,
        "safesearch": "1",
    }
    if news:
        params["time_range"] = "month"
    wiki: asyncio.Task[Message] | None = None
    try:
        async with asyncio.timeout_at(deadline):
            async with state.slots:
                await state.admit()
                wiki = asyncio.create_task(
                    engine_search(
                        state,
                        endpoint,
                        "wikipedia",
                        {k: v for k, v in params.items() if k != "time_range"},
                        policy,
                        deadline,
                    )
                )
                web_rows: dict[str, Message] = {}
                for index, engine in enumerate(policy.engines):
                    result = await engine_search(
                        state, endpoint, engine, params, policy, deadline
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
                evidence.append(await asyncio.shield(wiki))
    except TimeoutError:
        evidence.append(
            {"engine": "scheduler", "outcome": "upstream_timeout", "results": []}
        )
    finally:
        if wiki is not None and not any(
            row.get("engine") == "wikipedia" for row in evidence
        ):
            if not wiki.done():
                wiki.cancel()
            try:
                evidence.append(await wiki)
            except asyncio.CancelledError:
                pass
    results: dict[str, Message] = {}
    for attempt in evidence:
        rows = attempt.get("results", [])
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    results.setdefault(str(row["link"]), row)
    outcomes = {str(attempt["outcome"]) for attempt in evidence}
    web_count = sum(row.get("engine") != "wikipedia" for row in results.values())
    outcome = (
        "partial_results"
        if results
        and (outcomes - {"ok", "empty_results"} or web_count < (policy.minimum or n))
        else "ok"
        if results
        else "upstream_blocked"
        if "upstream_blocked" in outcomes
        else "upstream_timeout"
        if "upstream_timeout" in outcomes
        else "empty_results"
    )
    # Wikipedia remains complementary even when the web already filled n slots.
    web = [row for row in results.values() if row.get("engine") != "wikipedia"][:n]
    wikipedia = [row for row in results.values() if row.get("engine") == "wikipedia"][
        :1
    ]
    output: Message = {
        "provider": "searxng",
        "language": language,
        "outcome": outcome,
        "results": web + wikipedia,
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
    logging.getLogger(__name__).info(
        "searxng_outcome %s",
        json.dumps(
            {
                k: v
                for k, v in output.items()
                if k not in {"results", "consulted_at", "message"}
            }
        ),
    )
    if results:
        cache.put(key, output, 3600 if outcome == "ok" else 60)
    return output
