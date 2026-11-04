"""Hosted tools of the Responses API, run by Atlas between model turns (owner decision 2026-11-02).

web_search uses the same search chain as the assistant. Strict privacy refuses it: no request
leaves the owned infrastructure in that mode. Searches are billed through the model tokens
that read their results, as in the assistant.
"""

import json
import logging
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

HOSTED_TOOLS = ("web_search", "web_search_preview")
HOSTED_NAMES = ("web_search", "web_fetch")
MAX_ROUNDS = 4
SEARCH_FUNCTION = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web. Returns up to 5 results with title, url and snippet. "
            "Cite the urls you use."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}


async def web_search(query: str, timeout: float, key_id: str) -> dict[str, Any]:
    """Results for the model, compact: title, url and snippet only."""
    from services.orchestrator.tools import Runtime, Search
    from services.orchestrator.cache import Cache

    runtime = Runtime(
        Cache(Path(os.environ.get("ATLAS_WEB_CACHE", "BRAIN/web-cache.sqlite"))),
        Decimal(0),
        query,
    )
    try:
        found = await runtime.search(
            Search(query=query[:4000], lang="en", n=5), timeout
        )
    except (ValueError, KeyError, TimeoutError, OSError):
        found = {"results": [], "outcome": "search_unavailable"}
    rows = found.get("results")
    results = [
        {
            "title": str(row.get("title", ""))[:300],
            "url": str(row.get("link", ""))[:2048],
            "snippet": str(row.get("snippet", ""))[:400],
        }
        for row in (rows if isinstance(rows, list) else [])[:5]
        if isinstance(row, dict) and row.get("link")
    ]
    logging.getLogger("atlas.api").info(
        json.dumps(
            {
                "event": "api_hosted_search",
                "key": key_id,
                "results": len(results),
                "provider": getattr(runtime, "_last_search_provider", None),
            }
        )
    )
    return {"results": results} if results else {"results": [], "error": "no_results"}


def search_query(arguments: str) -> str:
    try:
        value = json.loads(arguments)
    except ValueError:
        return ""
    return str(value.get("query", "")) if isinstance(value, dict) else ""


FETCH_FUNCTION = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": (
            "Read a web page whose exact address appears in the conversation or in earlier "
            "search results. Returns its text."
        ),
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
}


def fetch_url(arguments: str) -> str:
    try:
        value = json.loads(arguments)
    except ValueError:
        return ""
    return str(value.get("url", "")) if isinstance(value, dict) else ""


def domain_match(host: str, domains: list[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def fetch_refusal(
    url: str, seen: str, options: dict[str, Any], used: int
) -> str | None:
    """Error code when the page may not be read, as the Messages web_fetch tool reports it.

    Only an address already present in the conversation or in earlier search results can
    be read, so a page cannot make the model send data to an address of its choice.
    """
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "").lower()
    if not url.startswith(("http://", "https://")) or not host:
        return "invalid_input"
    if isinstance(options.get("max_uses"), int) and used >= options["max_uses"]:
        return "max_uses_exceeded"
    # Exact addresses only: a substring test would let https://a.co through when only
    # https://a.com/... was given.
    import re

    present = {
        found.rstrip(".,;:!?").rstrip("/")
        for found in re.findall(r"https?://[^\s\"'<>()\[\]{}\\]+", seen)
    }
    if url.rstrip("/") not in present:
        return "url_not_allowed"
    allowed = [str(d).lower() for d in options.get("allowed_domains") or []]
    blocked = [str(d).lower() for d in options.get("blocked_domains") or []]
    if (allowed and not domain_match(host, allowed)) or domain_match(host, blocked):
        return "url_not_allowed"
    return None


async def web_fetch(
    url: str, timeout: float, key_id: str, max_tokens: int | None
) -> dict[str, Any]:
    """Text of a page for the model, through the assistant's validated fetch."""
    from services.orchestrator.tools import Fetch, Runtime
    from services.orchestrator.cache import Cache

    runtime = Runtime(
        Cache(Path(os.environ.get("ATLAS_WEB_CACHE", "BRAIN/web-cache.sqlite"))),
        Decimal(0),
        "",
    )
    try:
        page = await runtime.fetch(Fetch(url=url[:4096]), timeout)
    except (ValueError, KeyError, TimeoutError, OSError, RuntimeError):
        page = None
    logging.getLogger("atlas.api").info(
        json.dumps({"event": "api_hosted_fetch", "key": key_id, "ok": page is not None})
    )
    if page is None:
        return {"url": url, "error": "url_not_accessible"}
    text = str(page.get("text", ""))
    if max_tokens:
        text = text[: max_tokens * 4]
    return {
        "url": str(page.get("url", url)),
        "text": text,
        "retrieved_at": str(page.get("consulted_at", "")),
    }
