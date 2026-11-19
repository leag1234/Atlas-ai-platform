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

# v6 (2026-11-19 afternoon): staging SearXNG leaves through its own pool of two IPs. Coordinator
# rule kept: at most one query every 8 s across all prototype processes, at most 25 per run, and no
# search at all after the first blocked outcome. Engines rotate over the staging list.
# Brave ("too many requests"), Qwant ("access denied") and DuckDuckGo (CAPTCHA) each blocked their first
# query in the v6 runs of 2026-11-19 afternoon; Startpage answered every time: it is the only engine left.
ENGINES = ("startpage",)
SEARCH_DISABLED = None
GLOBAL_SPACING = 8.0
MAX_PER_RUN = 25
BLOCK = re.compile(r"captcha|access denied|denied|too many requests|suspend|forbidden|blocked|429|403", re.I)
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
    """One search on one engine, rotating; an empty answer tries the next engine once. Stops for good at the first block."""
    global _turn
    region = {"fr": "fr-FR", "en": "en-US", "de": "de-DE"}.get(language, "en-US")
    tried = []
    for _ in range(2):
        with _engine_lock:
            if SEARCH_DISABLED and not STATE["stopped"]:
                STATE["stopped"] = SEARCH_DISABLED
            if STATE["stopped"] or STATE["count"] >= MAX_PER_RUN:
                tried.append({"outcome": "search_stopped", "why": STATE["stopped"] or "run_cap"})
                return {"query": query, "engine": None, "results": [], "tried": tried}
            live = [e for e in ENGINES if e not in STATE.setdefault("disabled", [])]
            if not live:
                STATE["stopped"] = "every engine failed"
                continue
            engine = live[_turn % len(live)]
            _turn += 1
            _global_slot()
            STATE["count"] += 1
            try:
                response = httpx.get(
                    searxng_url() + "/search",
                    params={"q": query, "format": "json", "engines": engine, "language": region, "safesearch": "1"},
                    timeout=20,
                )
                data = response.json()
            except (httpx.HTTPError, ValueError) as error:
                STATE["disabled"].append(engine)
                tried.append({"engine": engine, "outcome": type(error).__name__})
                continue
            unresponsive = data.get("unresponsive_engines") or []
            reason = " ".join(str(u[1]) for u in unresponsive if len(u) > 1)
            if response.status_code == 429 or (unresponsive and BLOCK.search(reason)):
                STATE["stopped"] = f"{engine}: {reason or 'http 429'}"
                tried.append({"engine": engine, "outcome": "blocked", "detail": STATE["stopped"]})
                return {"query": query, "engine": engine, "results": [], "tried": tried}
            if unresponsive:
                # A timeout or an engine error is not a block: leave that engine for the rest of the run.
                STATE["disabled"].append(engine)
                tried.append({"engine": engine, "outcome": "engine_error", "detail": reason[:120]})
                continue
        rows = [
            {"title": str(r.get("title", ""))[:200], "url": str(r.get("url", "")), "snippet": str(r.get("content", ""))[:400]}
            for r in data.get("results", [])
            if str(r.get("url", "")).startswith("http")
        ]
        tried.append({"engine": engine, "outcome": "ok", "results": len(rows)})
        if rows:
            return {"query": query, "engine": engine, "results": rows[:n], "tried": tried}
    return {"query": query, "engine": None, "results": [], "tried": tried}


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


WIKI = re.compile(r"^https?://([a-z]{2})\.(?:m\.)?wikipedia\.org/wiki/([^#?]+)")
ALT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.6; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.7",
    "Accept-Language": "fr,en;q=0.8",
}


def read(url: str) -> dict:
    """A page's text, with fallbacks: Wikipedia from the local mirror, a second try with other headers, a plain-text extraction."""
    if url.startswith("kiwix:"):
        return kiwix_fetch(url)
    wiki = WIKI.match(url)
    if wiki:
        from urllib.parse import unquote

        language, title = wiki.group(1), unquote(wiki.group(2)).replace("_", " ")
        found = kiwix_search(title, language, n=1)
        if found["results"]:
            page = kiwix_fetch(found["results"][0]["url"])
            if "error" not in page:
                page["requested_url"] = url
                return page
    page = fetch(url)
    if page.get("error") in ("http_403", "http_406", "http_429", "http_503", "extraction_empty", "ReadTimeout", "ConnectTimeout", "RemoteProtocolError"):
        second = fetch(url, headers=ALT_HEADERS, plain_fallback=True)
        if "error" not in second:
            second["fallback"] = "alt_headers"
            return second
        page["second_try"] = second.get("error")
    return page


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


def fetch(url: str, timeout: float = 20.0, headers: dict | None = None, plain_fallback: bool = False) -> dict:
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
            headers=headers or {"User-Agent": USER_AGENT, "Accept-Language": "en,fr;q=0.8"},
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
        if len(text) < 400 and plain_fallback:
            # Pages whose main text the extractor misses (spec tables, short product pages): plain text of the body.
            plain = re.sub(r"<(script|style|noscript|svg)[^>]*>[\s\S]*?</\1>", " ", html, flags=re.I)
            plain = re.sub(r"<(?:br|/p|/div|/tr|/li|/h\d)[^>]*>", "\n", plain, flags=re.I)
            plain = re.sub(r"</t[dh]>", " | ", plain, flags=re.I)
            plain = re.sub(r"<[^>]+>", " ", plain)
            import html as html_lib

            plain = html_lib.unescape(plain)
            plain = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in plain.splitlines() if line.strip())
            if len(plain) >= 800 and re.search(r"\d", plain):
                text = plain[:60000]
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

