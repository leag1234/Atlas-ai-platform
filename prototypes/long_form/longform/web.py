"""Web search on the staging SearXNG service and page reading.

Search goes to the SearXNG instance the staging orchestrator uses. The engine
policy mirrors services/orchestrator/searxng.py: one engine per call (never
"categories" with "engines"), a spacing between calls to the same engine, so the
prototype does not get the shared server IP blocked.
"""

from __future__ import annotations

import io
import ipaddress
import os
import re
import socket
import threading
import time
from urllib.parse import urlsplit

import httpx
import trafilatura

# Web search is disabled for the rest of the measurement (2026-11-19 09:28): duckduckgo answered
# CAPTCHA and mojeek "access denied" to this IP; startpage is left to staging users.
ENGINES = ("mojeek", "startpage")
SEARCH_DISABLED = "web search disabled after upstream blocks (2026-11-19)"
# Coordinator rule of 2026-11-19 after the staging search incident: at most one query
# every 8 s overall, at most 30 per run, and no search at all after the first blocked outcome.
GLOBAL_SPACING = 8.0
MAX_PER_RUN = 30
MAX_BYTES = 6_000_000
SKIP_HOSTS = re.compile(
    r"(youtube\.com|youtu\.be|facebook\.com|instagram\.com|tiktok\.com|x\.com|twitter\.com|"
    r"linkedin\.com|pinterest\.|reddit\.com|quora\.com|amazon\.)",
    re.I,
)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"

_engine_lock = threading.Lock()
_turn = 0


def searxng_url() -> str:
    """The staging SearXNG endpoint, resolved like atlas_ports() does."""
    explicit = os.environ.get("ATLAS_SEARXNG_URL")
    if explicit:
        return explicit
    base = 0
    for line in open("/opt/atlas-src/staging.env"):
        if line.startswith("ATLAS_PORT_BASE="):
            base = int(line.split("=", 1)[1].strip() or 0)
    return f"http://127.0.0.1:{8888 + base}"


STATE = {"stopped": None, "count": 0, "last": 0.0}
SLOT_FILE = "/opt/atlas-src/proto-longform/.search-slot"


def _global_slot() -> None:
    """One query every GLOBAL_SPACING seconds across all prototype processes (file lock)."""
    import fcntl

    with open(SLOT_FILE, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        try:
            last = float(handle.read().strip() or 0)
        except ValueError:
            last = 0.0
        wait = last + GLOBAL_SPACING - time.time()
        if wait > 0:
            time.sleep(wait)
        handle.seek(0)
        handle.truncate()
        handle.write(str(time.time()))
        handle.flush()
        fcntl.flock(handle, fcntl.LOCK_UN)
    STATE["last"] = time.time()


def search(query: str, language: str, n: int = 6) -> dict:
    """One search on one engine, rotating. Stops for good at the first blocked outcome."""
    global _turn
    region = {"fr": "fr-FR", "en": "en-US", "de": "de-DE"}.get(language, "en-US")
    with _engine_lock:
        if SEARCH_DISABLED and not STATE["stopped"]:
            STATE["stopped"] = SEARCH_DISABLED
        if STATE["stopped"] or STATE["count"] >= MAX_PER_RUN:
            return {"query": query, "engine": None, "results": [], "tried": [{"outcome": "search_stopped", "why": STATE["stopped"] or "run_cap"}]}
        _global_slot()
        engine = ENGINES[_turn % len(ENGINES)]
        _turn += 1
        STATE["count"] += 1
        try:
            response = httpx.get(
                searxng_url() + "/search",
                params={"q": query, "format": "json", "engines": engine, "language": region, "safesearch": "1"},
                timeout=20,
            )
            data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            return {"query": query, "engine": engine, "results": [], "tried": [{"engine": engine, "outcome": type(error).__name__}]}
        unresponsive = data.get("unresponsive_engines") or []
        if unresponsive or response.status_code == 429:
            STATE["stopped"] = f"{engine}: {unresponsive[0][1] if unresponsive else 'http 429'}"
            return {"query": query, "engine": engine, "results": [], "tried": [{"engine": engine, "outcome": "blocked", "detail": STATE["stopped"]}]}
    rows = [
        {"title": str(r.get("title", ""))[:200], "url": str(r.get("url", "")), "snippet": str(r.get("content", ""))[:400]}
        for r in data.get("results", [])
        if str(r.get("url", "")).startswith("http")
    ]
    return {"query": query, "engine": engine, "results": rows[:n], "tried": [{"engine": engine, "outcome": "ok", "results": len(rows)}]}


def kiwix_search(query: str, language: str, n: int = 2) -> dict:
    """The local Wikipedia mirror (Kiwix), through Atlas's own client."""
    import asyncio

    from services.orchestrator import kiwix

    try:
        _, hits = asyncio.run(kiwix.search(query, language, 20))
    except Exception as error:  # noqa: BLE001 - mirror missing for a language, timeout
        return {"query": query, "engine": "kiwix-" + language, "results": [], "tried": [{"outcome": type(error).__name__}]}
    rows = [{"title": h.title, "url": "kiwix:" + language + ":" + h.path, "snippet": ""} for h in hits[:n]]
    return {"query": query, "engine": "kiwix-" + language, "results": rows, "tried": [{"outcome": "ok", "results": len(rows)}]}


def kiwix_fetch(ref: str) -> dict:
    import asyncio

    from services.orchestrator import kiwix

    _, language, path = ref.split(":", 2)
    started = time.monotonic()
    try:
        final, body = asyncio.run(kiwix.article(path, 20))
    except Exception as error:  # noqa: BLE001
        return {"url": ref, "error": "kiwix_" + type(error).__name__}
    text = trafilatura.extract(body.decode("utf-8", errors="replace"), include_tables=True, favor_recall=True) or ""
    if len(text) < 300:
        return {"url": ref, "error": "extraction_empty"}
    return {
        "url": kiwix.public_url(language, final or path),
        "requested_url": ref,
        "title": kiwix.path_title(final or path) + " — Wikipedia (local mirror)",
        "text": text,
        "chars": len(text),
        "seconds": round(time.monotonic() - started, 2),
        "consulted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def read(url: str) -> dict:
    return kiwix_fetch(url) if url.startswith("kiwix:") else fetch(url)


def _public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            return False
    return True


def fetch(url: str, timeout: float = 20.0) -> dict:
    """Fetch and extract a page's main text. PDF through pypdf."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return {"url": url, "error": "invalid_url"}
    if SKIP_HOSTS.search(parts.hostname):
        return {"url": url, "error": "skipped_host"}
    if not _public(parts.hostname):
        return {"url": url, "error": "non_public_host"}
    started = time.monotonic()
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en,fr;q=0.8"},
            max_redirects=4,
        ) as client:
            with client.stream("GET", url) as response:
                if response.status_code != 200:
                    return {"url": url, "error": f"http_{response.status_code}"}
                final = str(response.url)
                if not _public(urlsplit(final).hostname or ""):
                    return {"url": url, "error": "non_public_redirect"}
                kind = response.headers.get("content-type", "")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        return {"url": url, "error": "too_large"}
    except httpx.HTTPError as error:
        return {"url": url, "error": type(error).__name__}
    text = ""
    if "pdf" in kind or final.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(bytes(body)))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:40])
        except Exception as error:  # noqa: BLE001 - any PDF parser failure
            return {"url": url, "error": "pdf_" + type(error).__name__}
    else:
        html = bytes(body).decode("utf-8", errors="replace")
        text = (
            trafilatura.extract(
                html,
                url=final,
                include_tables=True,
                include_comments=False,
                favor_recall=True,
            )
            or ""
        )
        title_match = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, re.I)
    if len(text) < 400:
        return {"url": url, "error": "extraction_empty"}
    title = ""
    if "pdf" not in kind:
        title = re.sub(r"\s+", " ", title_match.group(1)).strip()[:200] if title_match else ""
    return {
        "url": final,
        "requested_url": url,
        "title": title or parts.hostname,
        "text": text,
        "chars": len(text),
        "seconds": round(time.monotonic() - started, 2),
        "consulted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

