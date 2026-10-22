"""Small, provenance-preserving Wikimedia Commons lookup for mechanical drawings."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import asyncio
import html
import json
import logging
import re
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, HTTPRedirectHandler, build_opener
from urllib.error import HTTPError, URLError

import aiohttp


_LOGGER = logging.getLogger(__name__)


def plain_metadata(value: str) -> str:
    text = html.unescape(re.sub(r"<[^>]*>", "", value))
    return re.sub(r"([\\`*_{}\[\]()<>!])", r"\\\1", " ".join(text.split()))


API = "https://commons.wikimedia.org/w/api.php"
ALLOWED_HOSTS = {"commons.wikimedia.org", "upload.wikimedia.org"}


@dataclass(frozen=True)
class CommonsDrawing:
    title: str
    image_url: str
    page_url: str
    author: str
    license: str


def allowed_url(value: str) -> bool:
    return (
        urlparse(value).scheme == "https" and urlparse(value).hostname in ALLOWED_HOSTS
    )


def ranked_titles(rows: list[object], subject: str) -> list[str]:
    """Require the named object and reject unrelated appearance-search results."""
    # Rear/front and the vehicle qualifier are often absent from component titles.
    words = set(re.findall(r"[a-z0-9]+", subject.lower()))
    words -= {"rear", "front", "hand", "bicycle"}
    if not words:
        words = set(re.findall(r"[a-z0-9]+", subject.lower()))
    rejected = re.compile(
        r"\b(?:p[\s-]*v|t[\s-]*s|graphs?|curves?|charts?|plots?|maps?|logos?|"
        r"portraits?|people|persons?|m[ae]n|wom[ae]n)\b",
        re.I,
    )
    preferred = re.compile(
        r"\b(?:schematic|cutaway|exploded|cross[\s-]*section|diagram)\b", re.I
    )
    ranked: list[tuple[int, str]] = []
    for row in rows:
        title = row.get("title") if isinstance(row, dict) else None
        if (
            not isinstance(title, str)
            or not title.startswith("File:")
            or not title.lower().endswith((".svg", ".png", ".gif", ".jpg", ".jpeg"))
            or rejected.search(title)
        ):
            continue
        title_words = set(re.findall(r"[a-z0-9]+", title.lower()))
        if words <= title_words:
            ranked.append((len(preferred.findall(title)), title))
    return [title for _, title in sorted(ranked, key=lambda item: -item[0])]


def parse_imageinfo(
    data: object, titles: list[str] | None = None
) -> CommonsDrawing | None:
    if not isinstance(data, dict):
        return None
    query = data.get("query")
    if not isinstance(query, dict):
        return None
    pages = query.get("pages", {})
    if not isinstance(pages, dict):
        return None
    ordered = list(pages.values())
    if titles is not None:
        ordered = [
            page
            for title in titles
            for page in pages.values()
            if isinstance(page, dict) and page.get("title") == title
        ]
    for page in ordered:
        if not isinstance(page, dict) or not isinstance(page.get("imageinfo"), list):
            continue
        info = page["imageinfo"][0] if page["imageinfo"] else {}
        metadata = info.get("extmetadata", {}) if isinstance(info, dict) else {}
        image = info.get("url") if isinstance(info, dict) else None

        def value(name: str) -> object:
            entry = metadata.get(name) if isinstance(metadata, dict) else None
            return entry.get("value") if isinstance(entry, dict) else None

        author = value("Artist")
        license_name = value("LicenseShortName")
        page_url = page.get("canonicalurl") or page.get("fullurl")
        title = page.get("title")
        if not all(
            isinstance(value, str) and value
            for value in (image, author, license_name, page_url, title)
        ):
            continue
        assert isinstance(image, str)
        assert isinstance(author, str)
        assert isinstance(license_name, str)
        assert isinstance(page_url, str)
        assert isinstance(title, str)
        if allowed_url(image) and allowed_url(page_url):
            return CommonsDrawing(
                plain_metadata(title),
                quote(image, safe=":/%?=&"),
                quote(page_url, safe=":/%?=&"),
                plain_metadata(author),
                plain_metadata(license_name),
            )
    return None


async def find_drawing(
    question: str,
    record_url: Callable[[str], None],
    request: Callable[[str], Awaitable[object]] | None = None,
    *,
    record_response: Callable[[str, object], None] | None = None,
) -> CommonsDrawing | None:
    """Look up a short object name, at most two requests and eight seconds total."""
    subject = " ".join(question.split())
    subject = re.sub(
        r"\b(?:alpha|beta|gamma)(?:[- ]type)?\b|\b(?:type|cutaway|schematic|diagram|animation)\b|\bcross[- ]section\b",
        "",
        subject,
        flags=re.I,
    )
    subject = " ".join(subject.split())
    if not re.fullmatch(r"[A-Za-z0-9 -]{2,80}", subject):
        _LOGGER.warning("commons_lookup reason=missing_subject candidates=0")
        return None
    query = subject + " diagram"

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(
            self,
            req: object,
            fp: object,
            code: int,
            msg: str,
            headers: object,
            newurl: str,
        ) -> None:
            return None

    def read(url: str) -> object:
        # The same Commons requests returned HTTP 403 with aiohttp and 200 with urllib.
        opener = build_opener(NoRedirect())
        try:
            with opener.open(
                Request(
                    url,
                    headers={
                        "User-Agent": "ATLAS-0/1.0 (mechanical drawing attribution)"
                    },
                ),
                timeout=8,
            ) as response:
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError("commons_response_size")
                return json.loads(raw)
        except HTTPError as exc:
            _LOGGER.warning("commons_http status=%d", exc.code)
            return {}

    async def get(url: str) -> object:
        return await asyncio.to_thread(read, url)

    async def fetch(parameters: dict[str, str | int]) -> object:
        url = API + "?" + urlencode({"action": "query", "format": "json", **parameters})
        record_url(url)
        result = await (request or get)(url)
        if record_response:
            record_response(url, result)
        return result

    try:
        async with asyncio.timeout(8):
            results = await fetch(
                {"list": "search", "srnamespace": 6, "srlimit": 4, "srsearch": query}
            )
            result_query = results.get("query", {}) if isinstance(results, dict) else {}
            rows = (
                result_query.get("search", []) if isinstance(result_query, dict) else []
            )
            rows = rows if isinstance(rows, list) else []
            _LOGGER.warning("commons_lookup query=%s candidates=%d", query, len(rows))
            titles = ranked_titles(rows[:4], subject)
            if not titles:
                return None
            data = await fetch(
                {
                    "titles": "|".join(titles),
                    "prop": "imageinfo|info",
                    "iiprop": "url|extmetadata",
                    "inprop": "url",
                }
            )
            drawing = parse_imageinfo(data, titles)
            _LOGGER.warning("commons_selection found=%s", bool(drawing))
            if drawing:
                record_url(drawing.image_url)
                record_url(drawing.page_url)
            return drawing
    except (aiohttp.ClientError, URLError, TimeoutError, ValueError):
        _LOGGER.warning("commons_lookup reason=request_failed candidates=0")
        return None
