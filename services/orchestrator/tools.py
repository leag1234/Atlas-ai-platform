"""Strict tool arguments; all untrusted results cross one explicit envelope."""

from packages.privacy import require_shared
from packages.limits import LimitError
from packages.ports import atlas_ports

import asyncio
import json
from urllib.parse import urlsplit
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from services.orchestrator.connectors import Selection

import aiohttp
from packages.validation import describe_validation

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.orchestrator import mcp_client, search_freshness, search_guard
from services.orchestrator.cache import Cache
from services.orchestrator.daily_budget import BudgetRefusal
from services.orchestrator.calculator import calculate
from services.orchestrator.datecalc import DEFAULT_ZONE
from services.orchestrator.content import hierarchical_summary
from packages.evidence import estimated_tokens
from packages.web_extract import extract
from services.orchestrator.loop import Call, Message, Reservation
from services.orchestrator.web import (
    MAX_BYTES,
    SEARCH_TIMEOUT,
    Web,
    http_timeout,
    validate_url,
)


class SearchQuotaError(ValueError):
    """Provider quota response without retaining credentials or response bodies."""

    def __init__(
        self, status: int, code: str = "quota_exceeded", retry_after: float = 0
    ) -> None:
        self.status = status
        self.retry_after = retry_after
        super().__init__(code)


SEARCH_PROVIDERS = frozenset({"serpapi", "tavily", "searxng"})
SHORT_COOLDOWN_SECONDS = 5
# Circuit breaker per provider. After a quota or rate refusal the provider is
# not asked again for its Retry-After window, or for these defaults: a used-up
# quota is checked again hourly (the owner may add credits), a rate limit
# after a minute. While open, no time is kept for the provider, so SearXNG has
# the whole search budget (staging 2026-11-15), and its refusal is still the
# reported cause (D5). Until 2026-11-16 an exhausted provider was asked again
# on every search.
QUOTA_BREAK_SECONDS = 3600
RATE_BREAK_SECONDS = 60
MAX_BREAK_SECONDS = 86400


@dataclass(frozen=True)
class Breaker:
    until: float
    code: str
    status: int  # Provider HTTP status; 0 for a local reservation limit.
    used: int = 0
    limit: int = 0

    def refusal(self) -> "SearchQuotaError | LimitError":
        if self.status:
            return SearchQuotaError(self.status, self.code)
        return LimitError(self.code, self.used, self.limit, "monthly search credits")


_BREAKERS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, Breaker]] = (
    WeakKeyDictionary()
)
# Searches of paid providers in flight, shared by one serving event loop.
_FLIGHTS: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, "asyncio.Task[Message]"]
] = WeakKeyDictionary()
# One logical search counts once against the caps, whatever the chain asks.
_ADMITTED: ContextVar[list[bool] | None] = ContextVar(
    "atlas_search_admitted", default=None
)


def provider_breakers() -> dict[str, Breaker]:
    """Open circuit breakers per provider, shared by one serving event loop."""
    return _BREAKERS.setdefault(asyncio.get_running_loop(), {})


def break_seconds(code: str, retry_after: float = 0) -> float:
    if retry_after > 0:
        return min(retry_after, MAX_BREAK_SECONDS)
    return RATE_BREAK_SECONDS if code == "search_rate_limited" else QUOTA_BREAK_SECONDS


async def shared_flight(
    key: str,
    work: Callable[[], Awaitable[Message]],
    timeout: float,
    start: Callable[[], None],
) -> Message:
    """Identical paid searches arriving together share one upstream call.

    ``start`` (caps and credit reservations) runs in the caller that starts
    the call, so a refusal of that caller never reaches the others. A
    cancelled waiter does not cancel the others; the call keeps its own
    provider timeout."""
    flights = _FLIGHTS.setdefault(asyncio.get_running_loop(), {})
    if key not in flights:
        start()
        task = asyncio.ensure_future(work())
        flights[key] = task
        task.add_done_callback(lambda done: flights.pop(key, None))
    else:
        logging.getLogger(__name__).warning(
            "search_cache %s", json.dumps({"result": "shared"})
        )
    async with asyncio.timeout(max(0.001, timeout)):
        return await asyncio.shield(flights[key])


def search_providers(value: str | None = None) -> tuple[str, ...]:
    """Return the configured, ordered search-provider chain."""
    raw = (
        value
        if value is not None
        else os.environ.get("ATLAS_SEARCH_PROVIDER", "serpapi")
    )
    providers = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    if not providers or any(provider not in SEARCH_PROVIDERS for provider in providers):
        raise ValueError("invalid_search_provider")
    return providers


def searxng_cooldown_wait(result: Message) -> float:
    """Return a safe local cooldown for the highest-priority blocked engine."""
    if result.get("outcome") != "upstream_blocked":
        return 0
    engines = result.get("engines")
    if not isinstance(engines, list):
        return 0
    for engine in engines:
        if not isinstance(engine, dict) or engine.get("outcome") != "upstream_blocked":
            continue
        if engine.get("reason") != "cooldown":
            return 0
        retry_after = engine.get("retry_after")
        if (
            isinstance(retry_after, (int, float))
            and 0 < retry_after <= SHORT_COOLDOWN_SECONDS
        ):
            return float(retry_after)
        return 0
    return 0


def paid_search_ttl(request: "Search") -> int:
    """A paid answer is kept as long as SearXNG's: each repeat costs a credit."""
    return search_freshness.cache_ttl(
        bound=search_freshness.time_bound(request.query, request.news),
        degraded=False,
    )


def paid_search_key(cache: Cache, tag: str, request: "Search") -> str:
    """Cache and flight identity of a paid search: near-identical queries of
    one language (case, accents, punctuation, stop words) share the entry,
    as SearXNG's do (search_freshness.normalized_query)."""
    return cache.key(
        [
            tag,
            search_freshness.normalized_query(request.query, request.lang),
            request.lang,
            request.n,
            request.news,
        ]
    )


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Calculator(Arguments):
    expr: str = Field(min_length=1, max_length=512)


class Search(Arguments):
    query: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    n: int = Field(default=5, ge=1, le=5)
    lang: Literal["fr", "de", "es", "it", "en"]
    news: bool = False


class Fetch(Arguments):
    url: str = Field(min_length=1, max_length=4096)
    query: str = Field(default="", max_length=32000)


class Rag(Arguments):
    query: str = Field(min_length=1, max_length=4000, pattern=r"\S")


SCHEMAS: dict[str, type[Arguments]] = {
    "calculator": Calculator,
    "web_search": Search,
    "web_fetch": Fetch,
    "rag_search": Rag,
}


# Pages that rarely state a fact a search was made for: social network posts,
# login walls and machine-translation front ends (owner test 2026-11-16: the
# first search of a timetable question returned an Instagram post, a Facebook
# page and a translation page). Dropped from a live result unless the query
# names that service, and only while other results remain.
LOW_VALUE_HOSTS: dict[str, tuple[str, ...]] = {
    "instagram.com": ("instagram",),
    "facebook.com": ("facebook",),
    "fb.com": ("facebook",),
    "tiktok.com": ("tiktok",),
    "pinterest": ("pinterest",),
    "x.com": ("twitter", "x.com", "tweet"),
    "twitter.com": ("twitter", "tweet"),
    "threads.net": ("threads",),
    "threads.com": ("threads",),
    "snapchat.com": ("snapchat",),
    "translate.google": ("google translate", "traduction google", "translate"),
    "translate.goog": ("google translate", "translate"),
    "deepl.com": ("deepl",),
    "reverso.net": ("reverso",),
    "linguee": ("linguee",),
}


def low_value_host(link: str, query: str) -> bool:
    host = urlsplit(link).netloc.lower().split("@")[-1].split(":")[0]
    labels = host.split(".")
    asked = query.casefold()
    for domain, names in LOW_VALUE_HOSTS.items():
        if "." in domain:
            matched = host == domain or host.endswith("." + domain)
            if domain == "translate.google":
                matched = ".".join(labels[:2]) == domain or host.startswith(
                    domain + "."
                )
        else:
            # A brand on any country domain (pinterest.fr, linguee.de).
            matched = domain in labels
        if matched:
            return not any(name in asked for name in names)
    return False


def drop_low_value(result: Message, query: str) -> Message:
    rows = result.get("results")
    if not isinstance(rows, list):
        return result
    kept = [
        row
        for row in rows
        if not (
            isinstance(row, dict)
            and isinstance(row.get("link"), str)
            and low_value_host(row["link"], query)
        )
    ]
    if not kept or len(kept) == len(rows):
        return result
    return {**result, "results": kept}


def declarations() -> list[Message]:
    descriptions = json.loads(Path("prompts/tools.json").read_text())
    if set(descriptions) != set(SCHEMAS) or not all(
        isinstance(v, str) for v in descriptions.values()
    ):
        raise ValueError("invalid_tool_descriptions")
    result: list[Message] = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": descriptions[name],
                "parameters": schema.model_json_schema(),
            },
        }
        for name, schema in SCHEMAS.items()
    ]
    from services.orchestrator.api_key_edge import key_call

    if (
        os.environ.get("ATLAS_MCP_CONFIG")
        and os.environ.get("ATLAS_MCP_CONNECTORS") != "1"
        and not key_call.get()
    ):
        result.append(mcp_client.declaration())
    return result


class Runtime:
    def __init__(self, cache: Cache, search_cost: Decimal, question: str = "") -> None:
        self.cache, self.search_cost = cache, search_cost
        Reservation(0, search_cost)
        self.web = Web()
        self.question = question
        self.connectors: Selection | None = None
        self._last_search_attempts: list[Message] = []
        # Breakers this search opened: provider -> seconds (search_chain log).
        self._last_search_breakers: dict[str, int] = {}
        self.daily_lang = "en"
        self.search_language: str | None = None
        # The person's own zone: "here" and "today" for the date_time tool.
        self.home_zone = DEFAULT_ZONE
        # Who searches, for the search caps and test traffic (search_guard):
        # the request's other identities and its interface conversation.
        self.search_identities: tuple[str, ...] = ()
        self.search_conversation = ""

    def search_scope(self) -> search_guard.Scope:
        return search_guard.scope(
            str(getattr(self, "daily_user", "unknown")),
            self.search_conversation,
            self.search_identities,
        )

    def _admit_upstream(self) -> None:
        """Count this logical search against its caps before upstream work."""
        admitted = _ADMITTED.get()
        if admitted is not None and admitted[0]:
            return
        search_guard.admit(
            getattr(self, "daily_ledger", None), self.search_scope(), self.daily_lang
        )
        if admitted is not None:
            admitted[0] = True

    def _admitted_reserve(self, provider: str) -> None:
        self._admit_upstream()
        self._reserve_search(provider)

    def estimate(self, call: Call) -> Reservation:
        provider = next(
            (
                candidate
                for candidate in search_providers()
                if candidate == "searxng"
                or os.environ.get(
                    "TAVILY_API_KEY" if candidate == "tavily" else "SERPAPI_KEY", ""
                )
            ),
            "serpapi",
        )
        multiplier = 2 if provider == "tavily" else 1
        return Reservation(
            0,
            self.search_cost * multiplier
            if call.name in {"web_search", "web_images"}
            else Decimal(0),
        )

    async def search(self, request: Search, timeout: float) -> Message:
        require_shared("web_search")  # strict privacy: no web search (packages.privacy)
        started = asyncio.get_running_loop().time()
        self._last_search_attempts = []
        self._last_search_breakers = {}
        attempts: object = None
        scope = self.search_scope()
        token = _ADMITTED.set([False])
        try:
            replay = search_guard.replay(scope.test)
            if replay is not None:
                found = replay.lookup(request.query, request.lang, request.news)
                if found is not None:
                    attempts = [{"provider": "replay", "outcome": "ok"}]
                    return found
                if not replay.live_on_miss:
                    attempts = [{"provider": "replay", "outcome": "replay_miss"}]
                    raise ValueError("search_replay_miss")
            result = drop_low_value(await self._search(request, timeout), request.query)
            attempts = result.get("attempts")
            if scope.test:
                try:
                    search_guard.record(
                        request.query, request.lang, request.news, result
                    )
                except OSError:
                    logging.getLogger(__name__).warning("search_record_failed")
            return result
        finally:
            _ADMITTED.reset(token)
            attempts = attempts or self._last_search_attempts
            if isinstance(attempts, list) and (
                len(attempts) > 1
                or scope.test
                or self._last_search_breakers
                or any(
                    isinstance(attempt, dict)
                    and (
                        "breaker" in attempt
                        or attempt.get("provider") == "replay"
                        or str(attempt.get("outcome", "")).endswith("daily_search")
                    )
                    for attempt in attempts
                )
            ):
                # Provider names and outcome codes only, never the query.
                logging.getLogger(__name__).warning(
                    json.dumps(
                        {
                            "event": "search_chain",
                            "attempts": attempts,
                            "seconds": round(
                                asyncio.get_running_loop().time() - started, 2
                            ),
                            **({"test": True} if scope.test else {}),
                            **(
                                {"breakers_opened": self._last_search_breakers}
                                if self._last_search_breakers
                                else {}
                            ),
                        }
                    )
                )

    async def _search(self, request: Search, timeout: float) -> Message:
        require_shared("web_search")  # strict privacy: no web search (packages.privacy)
        providers = search_providers()
        if self.search_scope().test:
            # Test traffic leaves the metered providers to real users.
            providers = search_guard.test_providers(providers)
        available = tuple(
            provider
            for provider in providers
            if provider == "searxng"
            or os.environ.get(
                "TAVILY_API_KEY" if provider == "tavily" else "SERPAPI_KEY", ""
            )
        )
        if not available:
            if len(providers) == 1 and providers[0] == "serpapi":
                raise KeyError("SERPAPI_KEY")
            raise ValueError("search_unavailable")
        last_quota: SearchQuotaError | None = None
        last_budget: BudgetRefusal | None = None
        last_limit: LimitError | None = None
        attempts: list[Message] = []
        last_searxng_result: Message | None = None
        clock = asyncio.get_running_loop().time
        deadline = clock() + timeout
        breakers = provider_breakers()

        def open_breaker(provider: str) -> Breaker | None:
            breaker = breakers.get(provider)
            if breaker is not None and breaker.until <= clock():
                breakers.pop(provider, None)
                return None
            return breaker

        def can_serve(provider: str) -> bool:
            # Decides whether time is kept for a later provider.
            return open_breaker(provider) is None and (
                self.cache.search_credits_left(
                    provider, credits=2 if provider == "tavily" else 1
                )
            )

        def trip(provider: str, exc: SearchQuotaError | LimitError) -> None:
            if isinstance(exc, SearchQuotaError):
                seconds = break_seconds(str(exc), exc.retry_after)
                breaker = Breaker(clock() + seconds, str(exc), exc.status)
            else:
                seconds = break_seconds(exc.code)
                breaker = Breaker(
                    clock() + seconds, exc.code, 0, exc.measured, exc.limit
                )
            breakers[provider] = breaker
            self._last_search_breakers[provider] = round(seconds)

        for index, provider in enumerate(available):
            remaining = deadline - clock()
            if remaining <= 0:
                break
            breaker = open_breaker(provider)
            if breaker is not None:
                # Skipped until its window ends; its refusal stays the
                # reported cause when no other provider serves (D5).
                attempts.append(
                    {
                        "provider": provider,
                        "outcome": breaker.code,
                        "breaker": "open",
                        "retry_in": round(breaker.until - clock()),
                    }
                )
                refusal = breaker.refusal()
                if isinstance(refusal, SearchQuotaError):
                    last_quota = refusal
                else:
                    last_limit = refusal
                continue
            # A later provider keeps enough time to answer: a paid provider takes 3
            # to 5 s, while slow SearXNG engines used to take all but 2 s of the
            # budget and the whole turn failed on the timeout (staging 2026-11-12).
            # No time is kept for providers that cannot answer.
            later = provider == "searxng" and any(
                can_serve(other) for other in available[index + 1 :]
            )
            reserve = max(2.05, min(7.0, remaining * 0.5)) if later else 0.0
            try:
                provider_timeout = max(0.001, remaining - reserve)
                result = await {
                    "serpapi": self.serpapi,
                    "tavily": self.tavily,
                    "searxng": self.searxng,
                }[provider](request, provider_timeout)
                outcome = str(result.get("outcome", "ok"))
                attempts.append({"provider": provider, "outcome": outcome})
                if provider == "searxng":
                    last_searxng_result = result
                cooldown = searxng_cooldown_wait(result) if provider == "searxng" else 0
                if cooldown:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining - reserve > cooldown + 0.05:
                        await asyncio.sleep(cooldown)
                        result = await self.searxng(
                            request,
                            deadline - asyncio.get_running_loop().time() - reserve,
                        )
                        outcome = str(result.get("outcome", "ok"))
                        attempts.append(
                            {"provider": provider, "outcome": outcome, "retry": True}
                        )
                        last_searxng_result = result
                if provider == "searxng" and outcome in {
                    "upstream_blocked",
                    "upstream_timeout",
                    "empty_results",
                }:
                    continue
                self._last_search_provider = provider
                self._last_search_attempts = attempts
                # Record attempts only when a fallback happened; a first-provider
                # success keeps the model-visible tool output unchanged.
                return {**result, "attempts": attempts} if len(attempts) > 1 else result
            except SearchQuotaError as exc:
                # A quota or rate refusal of one provider leaves the next
                # provider of the chain to answer; it is reported only when no
                # provider served (D5: never as an outage).
                last_quota = exc
                attempts.append({"provider": provider, "outcome": str(exc)})
                trip(provider, exc)
            except search_guard.SearchCapRefusal as exc:
                # A cap of this person, conversation or test traffic: no other
                # provider may answer either.
                attempts.append({"provider": provider, "outcome": exc.category})
                self._last_search_attempts = attempts
                raise
            except BudgetRefusal as exc:
                # The allowance of this provider is used up, not the search
                # itself: the next provider may still answer. Stopping here
                # told users "daily search limit" with a provider left unused
                # (staging 2026-11-11: SearXNG empty, Tavily allowance reached).
                last_budget = exc
                attempts.append({"provider": provider, "outcome": exc.category})
            except (TimeoutError, aiohttp.ClientError):
                attempts.append({"provider": provider, "outcome": "upstream_timeout"})
            except LimitError as exc:
                attempts.append({"provider": provider, "outcome": exc.code})
                if exc.code == "quota_exceeded":
                    # The local monthly reservation of this provider is full:
                    # like its own quota, the next provider may still answer.
                    last_limit = exc
                    trip(provider, exc)
                    continue
                self._last_search_attempts = attempts
                raise
            except ValueError as exc:
                if str(exc) != "search_unavailable":
                    attempts.append({"provider": provider, "outcome": str(exc)})
                    self._last_search_attempts = attempts
                    raise
                attempts.append({"provider": provider, "outcome": "search_unavailable"})
        if last_searxng_result is not None and all(
            str(attempt.get("provider")) == "searxng" for attempt in attempts
        ):
            outcome = str(attempts[-1]["outcome"])
            if outcome in {"upstream_blocked", "upstream_timeout", "empty_results"}:
                return {**last_searxng_result, "attempts": attempts}
        if last_quota is not None:
            self._last_search_attempts = attempts
            raise last_quota
        if last_budget is not None:
            self._last_search_attempts = attempts
            raise last_budget
        if last_limit is not None:
            self._last_search_attempts = attempts
            raise last_limit
        self._last_search_attempts = attempts
        raise ValueError("search_unavailable")

    def _reserve_search(self, provider: str) -> None:
        self.cache.reserve_search(provider, credits=2 if provider == "tavily" else 1)

    async def serpapi(self, request: Search, timeout: float) -> Message:
        key = paid_search_key(self.cache, "serpapi-search-v2", request)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        return await shared_flight(
            key,
            lambda: self._serpapi_upstream(request, timeout, key),
            timeout,
            lambda: self._admitted_reserve("serpapi"),
        )

    async def _serpapi_upstream(
        self, request: Search, timeout: float, key: str
    ) -> Message:
        from services.orchestrator.searxng import retry_after

        country, domain = {
            "fr": ("fr", "google.fr"),
            "de": ("de", "google.de"),
            "es": ("es", "google.es"),
            "it": ("it", "google.it"),
            "en": ("uk", "google.co.uk"),
        }[request.lang]
        params = {
            "engine": "google",
            "q": request.query,
            "hl": request.lang,
            "gl": country,
            "google_domain": domain,
            "api_key": os.environ["SERPAPI_KEY"],
        }
        if request.news:
            params["tbm"] = "nws"
        async with aiohttp.ClientSession(
            timeout=http_timeout(timeout, SEARCH_TIMEOUT), trust_env=False
        ) as session:
            async with session.get(
                "https://serpapi.com/search.json", params=params, allow_redirects=False
            ) as response:
                if response.status not in (200, 429):
                    raise ValueError("search_unavailable")
                body = bytearray()
                async for piece in response.content.iter_chunked(16384):
                    body.extend(piece)
                    if len(body) > MAX_BYTES:
                        raise LimitError(
                            "response_too_large", len(body), MAX_BYTES, "bytes"
                        )
                data = json.loads(body)
                if response.status == 429:
                    error = (
                        str(data.get("error", "")).lower()
                        if isinstance(data, dict)
                        else ""
                    )
                    code = (
                        "quota_exceeded"
                        if "run out of searches" in error
                        else "search_rate_limited"
                    )
                    raise SearchQuotaError(
                        response.status,
                        code,
                        retry_after(response.headers.get("Retry-After")),
                    )
        if not isinstance(data, dict) or data.get("error"):
            raise ValueError("search_unavailable")
        results = data.get("news_results" if request.news else "organic_results", [])
        if not isinstance(results, list):
            raise ValueError("invalid_search_response")
        items: list[Message] = []
        for item in results[: request.n]:
            if not isinstance(item, dict) or not isinstance(item.get("link"), str):
                raise ValueError("invalid_search_response")
            items.append(
                {
                    k: str(item.get(k, ""))[: 4096 if k == "link" else 300]
                    for k in ("title", "link", "snippet", "date")
                }
            )
        output: Message = {
            "provider": "serpapi",
            "results": items,
            "consulted_at": datetime.now(timezone.utc).isoformat(),
        }
        self.cache.put(key, output, paid_search_ttl(request))
        return output

    async def searxng(self, request: Search, timeout: float) -> Message:
        from packages.images import VisionMessage
        from services.orchestrator import searxng

        language = self.search_language or searxng.search_language(
            [VisionMessage(role="user", content=self.question)]
            if self.question.strip()
            else [],
            request.lang,
        )
        return await searxng.search(
            self.cache,
            os.environ.get("ATLAS_SEARXNG_URL", atlas_ports().url("searxng")),
            request.query,
            language,
            request.n,
            request.news,
            timeout,
            self._admit_upstream,
        )

    async def tavily(self, request: Search, timeout: float) -> Message:
        key = paid_search_key(self.cache, "tavily-advanced-contextual-v2", request)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        credential = os.environ.get("TAVILY_API_KEY", "")
        if not credential:
            raise ValueError("search_unavailable")
        # Tavily has a structured domain filter; site: text alone can return
        # unrelated pages. Preserve explicit user/model source restrictions.
        domains = re.findall(r"(?<!\S)site:([a-zA-Z0-9.-]+)(?=\s|$)", request.query)
        query = re.sub(r"(?<!\S)site:[a-zA-Z0-9.-]+(?=\s|$)", "", request.query).strip()
        for domain in domains:
            validate_url("https://" + domain)
        if not query:
            raise ValueError("invalid_search_query")
        return await shared_flight(
            key,
            lambda: self._tavily_upstream(
                request, timeout, key, credential, query, domains
            ),
            timeout,
            lambda: self._admitted_reserve("tavily"),
        )

    async def _tavily_upstream(
        self,
        request: Search,
        timeout: float,
        key: str,
        credential: str,
        query: str,
        domains: list[str],
    ) -> Message:
        from services.orchestrator.searxng import retry_after

        async with aiohttp.ClientSession(
            timeout=http_timeout(timeout, SEARCH_TIMEOUT), trust_env=False
        ) as session:
            async with session.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": credential,
                    "query": query,
                    "max_results": request.n,
                    "search_depth": "advanced",
                    "include_raw_content": True,
                    **({"include_domains": domains} if domains else {}),
                },
                allow_redirects=False,
            ) as response:
                if response.status in (429, 432):
                    raise SearchQuotaError(
                        response.status,
                        retry_after=retry_after(response.headers.get("Retry-After")),
                    )
                if response.status != 200:
                    raise ValueError("search_unavailable")
                body = bytearray()
                async for piece in response.content.iter_chunked(16384):
                    body.extend(piece)
                    if len(body) > MAX_BYTES:
                        raise LimitError(
                            "response_too_large", len(body), MAX_BYTES, "bytes"
                        )
                data = json.loads(body)
        if not isinstance(data, dict) or data.get("error"):
            raise ValueError("search_unavailable")
        results = data.get("results")
        if not isinstance(results, list):
            raise ValueError("invalid_search_response")
        consulted = datetime.now(timezone.utc).isoformat()
        items: list[Message] = []
        # Search previews reserve room for reasoning; full provider text remains
        # available through fetch and hierarchical synthesis.
        page_limit = min(16000, 32000 // max(1, min(len(results), request.n)))
        for result in results[: request.n]:
            if not isinstance(result, dict) or not all(
                isinstance(result.get(field), str)
                for field in ("url", "content", "title")
            ):
                raise ValueError("invalid_search_response")
            validate_url(result["url"])
            raw = result.get("raw_content")
            if raw is not None and not isinstance(raw, str):
                raise ValueError("invalid_search_response")
            source = raw or result["content"]
            text = source[:page_limit]
            items.append(
                {
                    "title": result["title"][:300],
                    "link": result["url"],
                    "snippet": result["content"][:page_limit],
                    "content": text,
                    "content_characters": len(source),
                    "preview_limit_characters": page_limit,
                    "truncated": len(source) > len(text),
                    "date": str(result.get("published_date", ""))[:100],
                }
            )
            # Reuse provider content if a model requests a discovered URL.
            # This path never performs a separate page fetch.
            self.cache.put(
                self.cache.key(["tavily-page", result["url"]]),
                {
                    "url": result["url"],
                    "text": source,
                    "consulted_at": consulted,
                    "provider": "tavily",
                    "truncated": False,
                },
                3600,
            )
        output: Message = {
            "provider": "tavily",
            "results": items,
            "consulted_at": consulted,
        }
        self.cache.put(key, output, paid_search_ttl(request))
        return output

    async def fetch(self, request: Fetch, timeout: float) -> Message:
        validate_url(request.url)
        cached = None
        if getattr(
            self, "_last_search_provider", None
        ) == "tavily" or search_providers() == ("tavily",):
            cached = self.cache.get(self.cache.key(["tavily-page", request.url]))
        key = self.cache.key(["fetch-full", request.url])
        if cached is None:
            cached = self.cache.get(key)
        if cached is None:
            url, body = await self.web.fetch(request.url, timeout)
            text = await asyncio.to_thread(extract, body, url)
            if not text:
                raise ValueError("extraction_empty")
            cached = {
                "url": url,
                "text": text,
                "consulted_at": datetime.now(timezone.utc).isoformat(),
            }
            self.cache.put(key, cached, 86400)
        text, url = str(cached["text"]), str(cached["url"])
        handle = self.cache.key(["source", url, text])
        self.cache.put(handle, {"text": text, "url": url}, 86400)
        output: Message = {
            "url": url,
            "consulted_at": cached["consulted_at"],
            "text": text,
            "handle": handle,
            "truncated": False,
            "selected": False,
            "passages": [{"start": 0, "end": len(text)}],
            "examined": 1,
            "token_budget": 4000,
            "estimated_tokens": estimated_tokens(text),
        }
        if "provider" in cached:
            output["provider"] = cached["provider"]
        if estimated_tokens(text) > 4000:
            summary, passages, sections = hierarchical_summary(
                text, request.query or self.question
            )
            output.update(
                text=summary,
                selected=True,
                passages=[{"start": p.start, "end": p.end} for p in passages],
                examined=passages[0].examined if passages else 0,
                estimated_tokens=estimated_tokens(summary),
                synthesis={
                    "method": "hierarchical_extractive",
                    "levels": 2,
                    "sections_examined": sections,
                    "source_characters": len(text),
                },
            )
        return output

    async def execute(self, call: Call, timeout: float) -> Message:
        from services.orchestrator.api_key_edge import key_call

        if key_call.get() and (
            call.name == "mcp_call"
            or call.name.startswith("mcp_")
            or call.name == "switch_connector_group"
        ):
            return {"error": "insufficient_scope", "missing_scope": "connectors"}
        if self.connectors is not None and (
            call.name.startswith("mcp_") or call.name == "switch_connector_group"
        ):
            return await self.connectors.execute(call, timeout)
        if call.name == "mcp_call":
            if os.environ.get("ATLAS_MCP_CONNECTORS") == "1":
                return {"error": "legacy_connector_disabled"}
            return await mcp_client.execute(call.arguments, timeout)
        from services.orchestrator import exact_tools, journey

        if call.name == journey.NAME:
            return await journey.execute(call.arguments, timeout)
        if call.name in exact_tools.SCHEMAS:
            return await exact_tools.execute(
                call.name, call.arguments, timeout, self.home_zone
            )
        from services.orchestrator import source_tools

        if call.name in source_tools.SCHEMAS:
            return await source_tools.execute(
                call.name,
                call.arguments,
                timeout,
                self.search_language or self.daily_lang,
            )
        from services.orchestrator.visual_tools import (
            VISUAL_SCHEMAS,
            ImageProviderError,
            MakeQR,
            WebImages,
            make_qr,
            web_images,
        )

        try:
            if call.name in VISUAL_SCHEMAS:
                visual = VISUAL_SCHEMAS[call.name].model_validate_json(call.arguments)
                async with asyncio.timeout(timeout):
                    if isinstance(visual, MakeQR):
                        result = await asyncio.to_thread(
                            make_qr, visual, self.daily_lang
                        )
                    else:
                        assert isinstance(visual, WebImages)
                        result = await web_images(
                            visual,
                            self.web,
                            self.cache,
                            self._admitted_reserve,
                            search_providers()[0],
                            timeout,
                            self.daily_lang,
                        )
                return {"trust": "untrusted", "data": result}
            schema = SCHEMAS.get(call.name)
            if schema is None:
                raise ValueError("unknown_tool")
            request = schema.model_validate_json(call.arguments)
            if isinstance(request, Calculator):
                return {"value": calculate(request.expr)}
            async with asyncio.timeout(timeout):
                if isinstance(request, Search):
                    result = await self.search(request, timeout)
                elif isinstance(request, Fetch):
                    result = await self.fetch(request, timeout)
                else:
                    process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "services.retrieval.tool",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    try:
                        body, _ = await process.communicate(call.arguments.encode())
                        if len(body) > 300000:
                            raise LimitError(
                                "retrieval_response_limit", len(body), 300000, "bytes"
                            )
                        if process.returncode:
                            raise ValueError("retrieval_unavailable")
                        result = json.loads(body)
                        if not isinstance(result, dict):
                            raise ValueError("invalid_retrieval_response")
                    finally:
                        if process.returncode is None:
                            process.kill()
                            await process.wait()
            return {"trust": "untrusted", "data": result}
        except ImageProviderError as exc:
            return {
                "error": exc.code,
                "provider": exc.provider,
                "provider_status": exc.status,
            }
        except SearchQuotaError as exc:
            return {
                "error": str(exc),
                "provider_status": exc.status,
                **(
                    {"attempts": self._last_search_attempts}
                    if call.name == "web_search" and len(self._last_search_attempts) > 1
                    else {}
                ),
            }
        except LimitError as exc:
            return {
                "error": exc.code,
                "message": exc.detail,
                **(
                    {"attempts": self._last_search_attempts}
                    if call.name == "web_search" and len(self._last_search_attempts) > 1
                    else {}
                ),
            }
        except ValidationError as exc:
            return {
                "error": "invalid_arguments",
                "message": describe_validation(exc),
                "schema": (
                    VISUAL_SCHEMAS.get(call.name) or SCHEMAS[call.name]
                ).model_json_schema(),
            }
        except TimeoutError:
            return {
                "error": "upstream_timeout"
                if call.name == "web_search" and "searxng" in search_providers()
                else "timeout",
                **(
                    {"attempts": self._last_search_attempts}
                    if call.name == "web_search" and len(self._last_search_attempts) > 1
                    else {}
                ),
            }
        except (aiohttp.ClientError, KeyError):
            return {
                "error": "provider_unavailable",
                **(
                    {"attempts": self._last_search_attempts}
                    if call.name == "web_search" and len(self._last_search_attempts) > 1
                    else {}
                ),
            }
        except ValueError as exc:
            return {
                "error": str(exc)
                if len(str(exc)) < 80
                else "invalid_provider_response",
                **(
                    {"attempts": self._last_search_attempts}
                    if call.name == "web_search" and len(self._last_search_attempts) > 1
                    else {}
                ),
            }
