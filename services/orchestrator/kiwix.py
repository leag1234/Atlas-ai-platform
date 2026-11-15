"""Local Wikipedia mirror: a kiwix-serve instance operated by Atlas (ZIM files).

kiwix-serve (https://github.com/kiwix/kiwix-tools, GPL-3.0) serves ZIM archives of
Wikipedia with their full-text index. Atlas runs it next to its other services, so a
lookup never leaves Atlas: this backend is allowed at every privacy level, strict
included. Configuration:

- ``ATLAS_KIWIX_URL``: base URL of the server (loopback or the instance network);
- ``ATLAS_KIWIX_BOOKS``: one book per language, ``fr=wikipedia_fr_all_nopic_2026-05,
  en=wikipedia_en_all_nopic_2026-06`` (the names kiwix-serve shows in its catalog).

The page URL returned for citation is the article's public Wikipedia address; the
result also names the local book, whose date says how fresh the copy is.
"""

from __future__ import annotations

import os
import re
import unicodedata
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlencode, urlsplit

import aiohttp

from services.orchestrator.web import USER_AGENT, http_timeout

TIMEOUT = 6.0
MAX_BYTES = 3_000_000
_BOOK = re.compile(r"[A-Za-z0-9_.-]{1,120}")


class KiwixError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class Hit:
    title: str
    path: str
    snippet: str


def configured() -> bool:
    return bool(os.environ.get("ATLAS_KIWIX_URL", "").strip()) and bool(books())


def books() -> dict[str, str]:
    found: dict[str, str] = {}
    for part in os.environ.get("ATLAS_KIWIX_BOOKS", "").split(","):
        language, _, book = part.strip().partition("=")
        if re.fullmatch(r"[a-z]{2,3}", language) and _BOOK.fullmatch(book):
            found[language] = book
    return found


def _base() -> str:
    base = os.environ.get("ATLAS_KIWIX_URL", "").strip().rstrip("/")
    if urlsplit(base).scheme not in {"http", "https"}:
        raise KiwixError(
            "local_mirror_unavailable", "ATLAS_KIWIX_URL is not an http URL"
        )
    return base


async def _get(path: str, timeout: float) -> bytes:
    limit = max(0.5, min(timeout, TIMEOUT))
    try:
        async with aiohttp.ClientSession(
            trust_env=False,
            headers={"User-Agent": USER_AGENT},
            timeout=http_timeout(limit, TIMEOUT),
        ) as session:
            async with session.get(_base() + path, allow_redirects=False) as response:
                if response.status == 404:
                    raise KiwixError("local_article_missing", "not in the local copy")
                if response.status != 200:
                    raise KiwixError(
                        "local_mirror_unavailable",
                        f"the local Wikipedia answered HTTP {response.status}",
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.content.iter_chunked(65_536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise KiwixError("local_mirror_unavailable", "page too large")
                return b"".join(chunks)
    except KiwixError:
        raise
    except (aiohttp.ClientError, TimeoutError, OSError):
        raise KiwixError(
            "local_mirror_unavailable", "the local Wikipedia could not be reached"
        ) from None


def parse_search(body: bytes) -> list[Hit]:
    """kiwix-serve's XML search results (RSS shape), best first."""
    if b"<!DOCTYPE" in body[:2000].upper() or b"<!ENTITY" in body.upper():
        raise KiwixError("local_mirror_unavailable", "unexpected search answer")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise KiwixError(
            "local_mirror_unavailable", "unreadable search answer"
        ) from None
    hits = []
    for item in root.iter("item"):
        title = " ".join((item.findtext("title") or "").split())
        path = (item.findtext("link") or "").strip()
        description = item.find("description")
        # Matched words come as <b> children of the description.
        snippet = "".join(description.itertext()) if description is not None else ""
        if title and path.startswith("/content/"):
            hits.append(Hit(title, path, " ".join(snippet.split())[:400]))
    return hits


def _folded(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", plain))


def best(hits: list[Hit], query: str) -> list[Hit]:
    """Hits with the article titled as the query first: full-text ranking can put a
    page that mentions the name many times (a museum named after a person) first."""
    wanted = _folded(query)

    def rank(hit: Hit) -> int:
        title = _folded(hit.title)
        if title == wanted:
            return 0
        if title.startswith(wanted + " ") or wanted.startswith(title + " "):
            return 1
        return 2

    return sorted(hits, key=rank)


class _Intro(HTMLParser):
    """Text of the article's first paragraphs, outside tables, notes and references."""

    SKIP = {"table", "style", "script", "figure", "nav", "aside"}

    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.limit, self.depth, self.in_p = limit, 0, 0
        self.parts: list[str] = []
        self.skipped: list[str] = []
        self.size = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = " ".join(value or "" for name, value in attrs if name == "class")
        # A footnote call is a <sup class="reference">; other <sup> are text (5e).
        if tag in self.SKIP or (tag == "sup" and "reference" in classes):
            self.depth += 1
            self.skipped.append(tag)
        elif tag == "p" and not self.depth:
            self.in_p += 1

    def handle_endtag(self, tag: str) -> None:
        if self.skipped and tag == self.skipped[-1]:
            self.skipped.pop()
            self.depth -= 1
        elif tag == "p" and self.in_p:
            self.in_p -= 1
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_p and not self.depth and self.size < self.limit:
            self.parts.append(data)
            self.size += len(data)


def introduction(html_body: bytes, limit: int) -> str:
    parser = _Intro(limit)
    parser.feed(html_body.decode("utf-8", errors="replace"))
    paragraphs = [" ".join(p.split()) for p in "".join(parser.parts).split("\n")]
    text = " ".join(p for p in paragraphs if p)
    return text[:limit]


def public_url(language: str, path: str) -> str:
    """The public Wikipedia address of a local article path (/content/<book>/[A/]Title)."""
    parts = path.split("/", 3)
    title = parts[3] if len(parts) > 3 else ""
    if title.startswith("A/"):
        title = title[2:]
    title = unquote(title.split("#", 1)[0])
    return f"https://{language}.wikipedia.org/wiki/" + quote(
        title.replace(" ", "_"), safe="_(),'-.:"
    )


async def search(query: str, language: str, timeout: float) -> tuple[str, list[Hit]]:
    book = books().get(language)
    if book is None:
        raise KiwixError("local_language_missing", f"no local Wikipedia in {language}")
    body = await _get(
        "/search?"
        + urlencode(
            {"books.name": book, "pattern": query, "format": "xml", "pageLength": "10"}
        ),
        timeout,
    )
    return book, parse_search(body)


async def article(path: str, timeout: float) -> bytes:
    if not path.startswith("/content/") or ".." in path:
        raise KiwixError("local_article_missing", "unexpected article path")
    return await _get(path, timeout)
