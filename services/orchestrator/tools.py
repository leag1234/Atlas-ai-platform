"""Strict tool arguments; all untrusted results cross one explicit envelope."""

from packages.limits import LimitError
from packages.ports import atlas_ports

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

import aiohttp
from packages.validation import describe_validation

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.orchestrator import mcp_client
from services.orchestrator.cache import Cache
from services.orchestrator.calculator import calculate
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

    def __init__(self, status: int, code: str = "quota_exceeded") -> None:
        self.status = status
        super().__init__(code)


SEARCH_PROVIDERS = frozenset({"serpapi", "tavily", "searxng"})
SHORT_COOLDOWN_SECONDS = 5


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
    if os.environ.get("ATLAS_MCP_CONFIG"):
        result.append(mcp_client.declaration())
    return result


class Runtime:
    def __init__(self, cache: Cache, search_cost: Decimal, question: str = "") -> None:
        self.cache, self.search_cost = cache, search_cost
        Reservation(0, search_cost)
        self.web = Web()
        self.question = question
        self._last_search_attempts: list[Message] = []
        self.daily_lang = "en"
        self.search_language: str | None = None

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
        providers = search_providers()
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
        attempts: list[Message] = []
        last_searxng_result: Message | None = None
        deadline = asyncio.get_running_loop().time() + timeout
        for provider in available:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                provider_timeout = remaining
                # Leave room for a short local cooldown and a later provider.
                if provider == "searxng" and len(available) > 1:
                    provider_timeout = max(0.001, remaining - 2.05)
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
                    if remaining > cooldown + 0.05:
                        await asyncio.sleep(cooldown)
                        result = await self.searxng(
                            request, deadline - asyncio.get_running_loop().time()
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
                # Record attempts only when a fallback happened; a first-provider
                # success keeps the model-visible tool output unchanged.
                return {**result, "attempts": attempts} if len(attempts) > 1 else result
            except SearchQuotaError as exc:
                last_quota = exc
                attempts.append({"provider": provider, "outcome": str(exc)})
            except (TimeoutError, aiohttp.ClientError):
                last_quota = None
                attempts.append({"provider": provider, "outcome": "upstream_timeout"})
            except LimitError as exc:
                attempts.append({"provider": provider, "outcome": exc.code})
                self._last_search_attempts = attempts
                raise
            except ValueError as exc:
                if str(exc) != "search_unavailable":
                    attempts.append({"provider": provider, "outcome": str(exc)})
                    self._last_search_attempts = attempts
                    raise
                last_quota = None
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
        self._last_search_attempts = attempts
        raise ValueError("search_unavailable")

    def _reserve_search(self, provider: str) -> None:
        self.cache.reserve_search(provider, credits=2 if provider == "tavily" else 1)

    async def serpapi(self, request: Search, timeout: float) -> Message:
        key = self.cache.key(["serpapi-search-v1", 4096, request.model_dump()])
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        self._reserve_search("serpapi")
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
                    raise SearchQuotaError(response.status, code)
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
        self.cache.put(key, output, 3600)
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
        )

    async def tavily(self, request: Search, timeout: float) -> Message:
        key = self.cache.key(
            ["tavily-advanced-contextual-excerpts", request.model_dump()]
        )
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
        self._reserve_search("tavily")
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
                    raise SearchQuotaError(response.status)
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
        self.cache.put(key, output, 3600)
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
        if call.name == "mcp_call":
            return await mcp_client.execute(call.arguments, timeout)
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
                            self._reserve_search,
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
