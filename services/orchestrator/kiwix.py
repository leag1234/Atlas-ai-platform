"""Local Wikipedia mirror: a kiwix-serve instance operated by Atlas (ZIM files).

kiwix-serve (https://github.com/kiwix/kiwix-tools, GPL-3.0) serves ZIM archives of
Wikipedia with their full-text index. Atlas runs it next to its other services, so a
lookup never leaves Atlas: this backend is allowed at every privacy level, strict
included. Configuration:

- ``ATLAS_KIWIX_URL``: base URL of the server (loopback or the instance network);
- ``ATLAS_KIWIX_BOOKS``: one book per language, either a dated book name
  (``fr=wikipedia_fr_all_mini_2026-11``, as kiwix-serve names it) or an undated family
  (``fr=wikipedia_fr_all_mini``). A family is resolved to the newest book of that
  family the server lists in its own catalog (``/catalog/v2/entries``), so a monthly
  refresh of the ZIM files needs no settings change.

The page URL returned for citation is the article's public Wikipedia address; the
result also names the local book, whose date says how fresh the copy is.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlencode, urlsplit

import aiohttp

from services.orchestrator.web import USER_AGENT, http_timeout

TIMEOUT = 6.0
MAX_BYTES = 3_000_000
_BOOK = re.compile(r"[A-Za-z0-9_.-]{1,120}")
# Characters kept as they are in an article path (Wikipedia's own URL style).
_SAFE = "_(),'-.:"


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
    # packages.privacy.local_encyclopedia reads the same settings (tested together).
    return bool(os.environ.get("ATLAS_KIWIX_URL", "").strip()) and bool(books())


def books() -> dict[str, str]:
    found: dict[str, str] = {}
    for part in os.environ.get("ATLAS_KIWIX_BOOKS", "").split(","):
        language, _, book = part.strip().partition("=")
        if re.fullmatch(r"[a-z]{2,3}", language) and _BOOK.fullmatch(book):
            found[language] = book
    return found


_DATED = re.compile(r"_(\d{4}-\d{2})$")


def book_date(book: str) -> str:
    """The copy's month from the book name (``wikipedia_fr_all_mini_2026-11``), or ''.
    A family name is resolved to a dated book before this is read (``search``)."""
    match = _DATED.search(book)
    return match.group(1) if match else ""


def is_family(book: str) -> bool:
    """Whether a configured name is an undated family (``wikipedia_fr_all_mini``)."""
    return not _DATED.search(book)


# Seconds a read of the server's catalog is reused: a refresh adds a book at most once
# a month, and a lookup must not pay a catalog request each time.
CATALOG_TTL = 300.0
_catalog: tuple[float, str, dict[str, str]] | None = None
_ATOM = "{http://www.w3.org/2005/Atom}"


def parse_catalog(body: bytes) -> dict[str, str]:
    """The newest served book of every family in kiwix-serve's OPDS catalog: a book
    ``wikipedia_fr_all_mini_2026-11`` (the last part of its ``/content/`` link, the
    name the server serves it under) belongs to the family ``wikipedia_fr_all_mini``."""
    if b"<!DOCTYPE" in body[:2000].upper() or b"<!ENTITY" in body.upper():
        raise KiwixError("local_mirror_unavailable", "unexpected catalog answer")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise KiwixError(
            "local_mirror_unavailable", "unreadable catalog answer"
        ) from None
    newest: dict[str, str] = {}
    for entry in root.iter(f"{_ATOM}entry"):
        for link in entry.iter(f"{_ATOM}link"):
            href = link.get("href") or ""
            if link.get("type") != "text/html" or "/content/" not in href:
                continue
            book = href.rstrip("/").rsplit("/content/", 1)[1]
            match = _DATED.search(book)
            if not match or not _BOOK.fullmatch(book) or "/" in book:
                continue
            family = book[: match.start()]
            # Same family: the later month wins ("YYYY-MM" sorts as text).
            if family not in newest or book_date(newest[family]) < match.group(1):
                newest[family] = book
    return newest


def forget_catalog() -> None:
    global _catalog
    _catalog = None


async def resolve(language: str, timeout: float) -> str:
    """The served book for a language: a dated name as configured, a family as the
    newest book of that family in the server's catalog. A family the catalog does not
    list, or a catalog that cannot be read, makes the mirror unavailable for that
    language; nothing outside the mirror is asked."""
    global _catalog
    book = books().get(language)
    if book is None:
        raise KiwixError("local_language_missing", f"no local Wikipedia in {language}")
    if not is_family(book):
        return book
    base = _base()
    now = time.monotonic()
    if _catalog is None or _catalog[1] != base or now - _catalog[0] > CATALOG_TTL:
        _, body = await _fetch("/catalog/v2/entries?count=500", timeout)
        _catalog = (now, base, parse_catalog(body))
    served = _catalog[2].get(book)
    if served is None:
        raise KiwixError(
            "local_mirror_unavailable",
            f"the local Wikipedia serves no book of the family {book}",
        )
    return served


def _base() -> str:
    base = os.environ.get("ATLAS_KIWIX_URL", "").strip().rstrip("/")
    if urlsplit(base).scheme not in {"http", "https"}:
        raise KiwixError(
            "local_mirror_unavailable", "ATLAS_KIWIX_URL is not an http URL"
        )
    return base


_REDIRECTS = {301, 302, 303, 307, 308}
_HOPS = 3


def _redirect(location: str, base: str) -> str:
    """The /content/ path a redirect of the mirror points to (a ZIM redirect entry:
    ``Einstein`` to ``Albert_Einstein``); anything else is refused."""
    target, origin = urlsplit(location), urlsplit(base)
    same = not target.netloc or (
        target.scheme == origin.scheme and target.netloc == origin.netloc
    )
    prefix = origin.path.rstrip("/")
    path = target.path[len(prefix) :] if target.path.startswith(prefix) else ""
    if not same or not path.startswith("/content/") or ".." in path:
        raise KiwixError("local_mirror_unavailable", "unexpected redirect")
    return path


async def _fetch(path: str, timeout: float) -> tuple[str, bytes]:
    """The final path and body of a GET, following the mirror's own redirects."""
    limit = max(0.5, min(timeout, TIMEOUT))
    base = _base()
    try:
        async with aiohttp.ClientSession(
            trust_env=False,
            headers={"User-Agent": USER_AGENT},
            timeout=http_timeout(limit, TIMEOUT),
        ) as session:
            for _ in range(_HOPS + 1):
                async with session.get(base + path, allow_redirects=False) as response:
                    if response.status in _REDIRECTS:
                        path = _redirect(response.headers.get("Location", ""), base)
                        continue
                    if response.status == 404:
                        raise KiwixError(
                            "local_article_missing", "not in the local copy"
                        )
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
                            raise KiwixError(
                                "local_mirror_unavailable", "page too large"
                            )
                    return path, b"".join(chunks)
            raise KiwixError("local_mirror_unavailable", "too many redirects")
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


# Title markers of pages that are not an entity's article: disambiguation pages of the
# five editions, and namespaces (categories, portals) the full-text index also holds.
_LIST_PAGE = re.compile(
    r"\((?:homonymie|disambiguation|begriffsklarung|desambiguacion|disambigua)\)\s*$"
)
_NAMESPACE = re.compile(
    r"^(?:categor(?:y|ie|ia)|kategorie|portal|portail|wikipedia|aide|help|hilfe|"
    r"ayuda|aiuto|modele|template|vorlage|plantilla)\s*:"
)


def _plain_title(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in folded if not unicodedata.combining(c)).strip()


def best(hits: list[Hit], query: str) -> list[Hit]:
    """Hits with the article titled as the query first: full-text ranking can put a
    page that mentions the name many times (a museum named after a person, a school)
    first. The title as written (case aside) beats a title equal only once accents
    and punctuation are dropped (``Berlin`` before ``.berlin``, ``München`` before
    ``Munchen``). Disambiguation pages and namespaces (categories) come last."""
    wanted = _folded(query)

    def as_written(text: str) -> str:
        return " ".join(unicodedata.normalize("NFC", text).casefold().split())

    written = as_written(query)

    def rank(hit: Hit) -> tuple[int, int]:
        plain = _plain_title(hit.title)
        listing = int(bool(_LIST_PAGE.search(plain) or _NAMESPACE.match(plain)))
        title = _folded(hit.title)
        if as_written(hit.title) == written:
            return listing, 0
        if title == wanted:
            return listing, 1
        if title.startswith(wanted + " ") or wanted.startswith(title + " "):
            return listing, 2
        return listing, 3

    seen: set[str] = set()
    unique = []
    for hit in hits:
        if hit.path not in seen:
            seen.add(hit.path)
            unique.append(hit)
    return sorted(unique, key=rank)


def parse_suggestions(body: bytes, book: str) -> list[Hit]:
    """kiwix-serve's title suggestions (JSON), as hits on the book's article paths."""
    try:
        rows = json.loads(body)
    except ValueError:
        raise KiwixError(
            "local_mirror_unavailable", "unreadable suggestion answer"
        ) from None
    hits = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("kind") != "path":
            continue
        # kiwix-serve escapes both fields as HTML ("Jeanne d&apos;Arc").
        title, path = (
            unescape(str(row.get("value", ""))),
            unescape(str(row.get("path", ""))),
        )
        if title and path and ".." not in path and not path.startswith("/"):
            hits.append(Hit(title, f"/content/{book}/" + quote(path, safe=_SAFE), ""))
    return hits


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


def _path_name(path: str) -> str:
    parts = path.split("/", 3)
    name = parts[3] if len(parts) > 3 else ""
    if name.startswith("A/"):
        name = name[2:]
    return unquote(name.split("#", 1)[0])


def path_title(path: str) -> str:
    """The article title of a local path (/content/<book>/Albert_Einstein)."""
    return _path_name(path).replace("_", " ")


def public_url(language: str, path: str) -> str:
    """The public Wikipedia address of a local article path (/content/<book>/[A/]Title)."""
    return f"https://{language}.wikipedia.org/wiki/" + quote(
        _path_name(path).replace(" ", "_"), safe=_SAFE
    )


async def search(query: str, language: str, timeout: float) -> tuple[str, list[Hit]]:
    """Title suggestions first (the title index: "Albert Einstein" finds the article
    even when full-text ranking puts schools and prizes named after him first), then
    full-text hits when no title matches the query exactly. The returned book is the
    served (dated) name, a configured family resolved."""
    book = await resolve(language, timeout)
    try:
        _, body = await _fetch(
            "/suggest?" + urlencode({"content": book, "term": query, "count": "10"}),
            timeout,
        )
    except KiwixError:
        # The server may have been restarted on a newer book: read the catalog again.
        forget_catalog()
        raise
    hits = parse_suggestions(body, book)
    wanted = _folded(query)
    if any(_folded(hit.title) == wanted for hit in hits):
        return book, hits
    _, body = await _fetch(
        "/search?"
        + urlencode(
            {"books.name": book, "pattern": query, "format": "xml", "pageLength": "10"}
        ),
        timeout,
    )
    return book, hits + parse_search(body)


async def article(path: str, timeout: float) -> tuple[str, bytes]:
    """The article's final path (after a redirect entry) and its page."""
    if not path.startswith("/content/") or ".." in path:
        raise KiwixError("local_article_missing", "unexpected article path")
    return await _fetch(path, timeout)
