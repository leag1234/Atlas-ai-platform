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
