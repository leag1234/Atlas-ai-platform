"""Bounded GET requests to a fixed list of public reference services, with a memory cache.

The specialized sources (encyclopedia, news feeds, public statistics, French public
datasets) reach only the hosts listed here, over HTTPS, without redirects, with a
descriptive User-Agent that carries no personal data (the project URL only, as the
Wikimedia User-Agent policy asks), a per-host concurrency limit and a size ceiling.

Each call names the privacy capability it serves; ``require_shared`` refuses it before
anything is sent when the request's privacy level does not allow that capability
(packages.privacy): strict closes all of them, enhanced keeps only the downloads that
carry no request content (fixed news feeds and whole public datasets).
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Mapping
from urllib.parse import urlencode, urlsplit

import aiohttp

from packages.privacy import require_shared
from services.orchestrator.web import USER_AGENT, http_timeout

# Hosts reachable by the specialized sources. A Wikipedia language edition is any
# "<code>.wikipedia.org" whose code passed encyclopedia.wiki_language.
FIXED_HOSTS = frozenset(
    {
        "www.wikidata.org",
        "ec.europa.eu",
        "geo.api.gouv.fr",
        "www.data.gouv.fr",
        "static.data.gouv.fr",
    }
)
# Wikimedia asks clients to keep requests serial; other hosts get a small margin.
_PARALLEL = {"wikipedia.org": 2, "www.wikidata.org": 2}
_DEFAULT_PARALLEL = 4
REQUEST_TIMEOUT = 12.0
CACHE_ENTRIES = 256
CACHE_BYTES = 48_000_000


class SourceError(ValueError):
    """A reference service could not answer; never replaced by a guess."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def allowed_host(host: str, extra: frozenset[str] = frozenset()) -> bool:
    if host in FIXED_HOSTS or host in extra:
        return True
    labels = host.split(".")
    return (
        len(labels) == 3
        and labels[1:] == ["wikipedia", "org"]
        and labels[0].replace("-", "").isalnum()
        and 2 <= len(labels[0]) <= 12
    )


class _Memory:
    """A small LRU cache of response bodies, bounded in entries and bytes."""

    def __init__(self) -> None:
        self.entries: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self.size = 0

    def get(self, key: str) -> bytes | None:
        entry = self.entries.get(key)
        if entry is None:
            return None
        if entry[0] < time.monotonic():
            self.drop(key)
            return None
        self.entries.move_to_end(key)
        return entry[1]

    def put(self, key: str, body: bytes, ttl: float) -> None:
        self.drop(key)
        self.entries[key] = (time.monotonic() + ttl, body)
        self.size += len(body)
        while self.entries and (
            len(self.entries) > CACHE_ENTRIES or self.size > CACHE_BYTES
        ):
            self.drop(next(iter(self.entries)))

    def drop(self, key: str) -> None:
        entry = self.entries.pop(key, None)
        if entry is not None:
            self.size -= len(entry[1])


CACHE = _Memory()
_SLOTS: dict[tuple[int, str], asyncio.Semaphore] = {}


def _slot(host: str) -> asyncio.Semaphore:
    group = "wikipedia.org" if host.endswith(".wikipedia.org") else host
    # One semaphore per event loop: a semaphore waits on the loop it first used.
    key = (id(asyncio.get_running_loop()), group)
    if key not in _SLOTS:
        _SLOTS[key] = asyncio.Semaphore(_PARALLEL.get(group, _DEFAULT_PARALLEL))
    return _SLOTS[key]


async def get(
    url: str,
    *,
    capability: str,
    timeout: float,
    params: Mapping[str, str] | None = None,
    ttl: float = 3600,
    max_bytes: int = 2_000_000,
    extra_hosts: frozenset[str] = frozenset(),
) -> bytes:
    """The body of a GET to an allowed host; cached for ``ttl`` seconds."""
    require_shared(capability)
    full = url + ("?" + urlencode(params) if params else "")
    parts = urlsplit(full)
    host = parts.hostname or ""
    if parts.scheme != "https" or not allowed_host(host, extra_hosts):
        raise SourceError(
            "source_not_allowed", f"{host or 'unknown host'} is not a listed source"
        )
    cached = CACHE.get(full)
    if cached is not None:
        return cached
    limit = max(0.5, min(timeout, REQUEST_TIMEOUT))
    try:
        async with _slot(host):
            async with aiohttp.ClientSession(
                trust_env=False,
                headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
                timeout=http_timeout(limit, REQUEST_TIMEOUT),
            ) as session:
                async with session.get(full, allow_redirects=False) as response:
                    if response.status == 429:
                        raise SourceError(
                            "source_rate_limited",
                            f"{host} asked to slow down (HTTP 429)",
                        )
                    if response.status == 404:
                        raise SourceError(
                            "source_not_found", f"{host} answered HTTP 404"
                        )
                    if response.status != 200:
                        raise SourceError(
                            "source_unavailable",
                            f"{host} answered HTTP {response.status}",
                        )
                    # read(n) returns what has arrived, up to n bytes: read on to the
                    # end, or one byte past the ceiling.
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.content.iter_chunked(65_536):
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > max_bytes:
                            break
                    body = b"".join(chunks)
    except SourceError:
        raise
    except (aiohttp.ClientError, TimeoutError, OSError):
        raise SourceError(
            "source_unavailable", f"{host} could not be reached"
        ) from None
    if len(body) > max_bytes:
        raise SourceError(
            "source_too_large", f"{host} answered more than {max_bytes} bytes"
        )
    CACHE.put(full, body, ttl)
    return body
