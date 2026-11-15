"""Latest headlines from the public RSS and Atom feeds of a curated list of outlets.

French and European outlets first, public broadcasters preferred. Feeds are fixed URLs:
the request carries nothing from the conversation, the topic is matched here. Each feed
is kept ten minutes; items are filtered by the topic's words, deduplicated by link and
title, and returned newest first with outlet, date, link and a short description (the
use a feed reader makes of a feed: headline, date, link, attribution).

The outlet list is a default the operator can narrow with ATLAS_NEWS_OUTLETS (comma
list of outlet ids). Reuse terms differ between outlets; see docs/23.
"""

from __future__ import annotations

import asyncio
import email.utils
import os
import re
import unicodedata
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import urlsplit, urlunsplit

from packages.privacy import require_shared
from services.orchestrator import reference_fetch
from services.orchestrator.reference_fetch import SourceError

CAPABILITY = "news_feeds"
FEED_SECONDS = 600
MAX_FEED_BYTES = 3_000_000
SUMMARY_CHARS = 280


@dataclass(frozen=True)
class Outlet:
    id: str
    name: str
    language: str
    url: str
    public_service: bool = False


OUTLETS: tuple[Outlet, ...] = (
    Outlet(
        "franceinfo", "franceinfo", "fr", "https://www.franceinfo.fr/titres.rss", True
    ),
    Outlet(
        "franceinfo-eco",
        "franceinfo (économie)",
        "fr",
        "https://www.franceinfo.fr/economie.rss",
        True,
    ),
    Outlet("rfi", "RFI", "fr", "https://www.rfi.fr/fr/rss", True),
    Outlet("france24-fr", "France 24", "fr", "https://www.france24.com/fr/rss", True),
    Outlet(
        "rtbf",
        "RTBF",
        "fr",
        "https://rss.rtbf.be/article/rss/highlight_rtbf_info.xml",
        True,
    ),
    Outlet("lemonde", "Le Monde", "fr", "https://www.lemonde.fr/rss/une.xml"),
    Outlet(
        "lemonde-eco",
        "Le Monde (économie)",
        "fr",
        "https://www.lemonde.fr/economie/rss_full.xml",
    ),
    Outlet(
        "lemonde-intl",
        "Le Monde (international)",
        "fr",
        "https://www.lemonde.fr/international/rss_full.xml",
    ),
    Outlet(
        "lemonde-tech",
        "Le Monde (Pixels)",
        "fr",
        "https://www.lemonde.fr/pixels/rss_full.xml",
    ),
    Outlet(
        "lefigaro",
        "Le Figaro",
        "fr",
        "https://www.lefigaro.fr/rss/figaro_actualites.xml",
    ),
    Outlet(
        "liberation",
        "Libération",
        "fr",
        "https://www.liberation.fr/arc/outboundfeeds/rss/?outputType=xml",
    ),
    Outlet("ouestfrance", "Ouest-France", "fr", "https://www.ouest-france.fr/rss/une"),
    Outlet(
        "20minutes", "20 Minutes", "fr", "https://www.20minutes.fr/feeds/rss-une.xml"
    ),
    Outlet(
        "euronews-fr",
        "Euronews",
        "fr",
        "https://fr.euronews.com/rss?format=mrss&level=theme&name=news",
    ),
    Outlet("france24-en", "France 24", "en", "https://www.france24.com/en/rss", True),
    Outlet("dw-en", "DW", "en", "https://rss.dw.com/rdf/rss-en-all", True),
    Outlet(
        "bbc-world",
        "BBC News",
        "en",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        True,
    ),
    Outlet(
        "euronews-en",
        "Euronews",
        "en",
        "https://www.euronews.com/rss?format=mrss&level=theme&name=news",
    ),
    Outlet(
        "guardian-world", "The Guardian", "en", "https://www.theguardian.com/world/rss"
    ),
    Outlet("politico-eu", "Politico Europe", "en", "https://www.politico.eu/feed/"),
    Outlet(
        "tagesschau",
        "tagesschau",
        "de",
        "https://www.tagesschau.de/infoservices/alle-meldungen-100~rss2.xml",
        True,
    ),
    Outlet("dw-de", "DW", "de", "https://rss.dw.com/rdf/rss-de-all", True),
    Outlet(
        "spiegel", "Der Spiegel", "de", "https://www.spiegel.de/schlagzeilen/index.rss"
    ),
    Outlet(
        "elpais",
        "El País",
        "es",
        "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada",
    ),
    Outlet(
        "elmundo",
        "El Mundo",
        "es",
        "https://e00-elmundo.uecdn.es/elmundo/rss/portada.xml",
    ),
    Outlet(
        "ansa", "ANSA", "it", "https://www.ansa.it/sito/notizie/topnews/topnews_rss.xml"
    ),
    Outlet(
        "repubblica",
        "la Repubblica",
        "it",
        "https://www.repubblica.it/rss/homepage/rss2.0.xml",
    ),
)
HOSTS = frozenset(urlsplit(outlet.url).hostname or "" for outlet in OUTLETS)

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "rss1": "http://purl.org/rss/1.0/",
}
# Words that say "news" or "today" rather than the topic, and function words, in the
# interface languages: they never decide whether a headline matches.
_STOP = frozenset(
    """
    a au aux avec ce ces cette dans de des du en et la le les leur l d un une sur pour par
    qui que quoi quel quelle quels quelles est sont ou se sa son ses il elle on y ne pas
    plus moins tres the of and in on for to at by with from is are what whats which who
    about news latest last today todays tonight yesterday recent recently update updates
    headline headlines breaking story stories current currently happening happened going
    actu actus actualite actualites info infos information informations nouvelle nouvelles
    derniere dernieres dernier derniers aujourd aujourdhui hui hier ce matin soir semaine
    jour journee neuf passe passer quoi savoir donne donner dis dire moi me peux pouvez
    stp svp please tell give show me can could you any some there anything new
    nachrichten neuigkeiten aktuell aktuelle aktuellen heute gestern was gibt es neues der die das
    und ein eine zu im von mit uber
    noticias noticia hoy ayer ultimas ultima actualidad que hay sobre el los las del
    notizie notizia oggi ieri ultime ultima novita cosa ci sono il lo gli di della sul
    """.split()
)


class Item(dict[str, object]):
    pass


def plain(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.lower().replace("’", "'"))
    return "".join(c for c in folded if not unicodedata.combining(c))


def topic_words(topic: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", plain(topic))
    kept = [w for w in words if w not in _STOP and (len(w) > 2 or w.isdigit())]
    return list(dict.fromkeys(kept))[:8]


def _stem(word: str) -> str:
    # A crude, language-neutral stem: plural and gender endings rarely change the
    # first six letters (retraite/retraites, election/elections, Ukraine/ukrainien).
    return word[:6] if len(word) > 6 else word


def _text(node: ElementTree.Element | None) -> str:
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def _clean(html_text: str) -> str:
    text = re.sub(r"<[^>]{0,2000}>", " ", unescape(html_text))
    return " ".join(unescape(text).split())


def _date(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse(body: bytes, outlet: Outlet) -> list[Item]:
    """Items of an RSS 2.0, RSS 1.0 (RDF) or Atom feed; anything else is refused."""
    head = body[:4000].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in body.upper():
        raise SourceError(
            "feed_invalid", f"{outlet.name} feed declares a document type"
        )
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise SourceError(
            "feed_invalid", f"{outlet.name} feed is not well-formed XML"
        ) from None
    nodes = (
        root.findall("./channel/item")
        or root.findall("rss1:item", _NS)
        or root.findall("atom:entry", _NS)
    )
    items: list[Item] = []
    for node in nodes:
        title = _clean(
            _text(node.find("title"))
            or _text(node.find("rss1:title", _NS))
            or _text(node.find("atom:title", _NS))
        )
        link = _text(node.find("link")) or _text(node.find("rss1:link", _NS))
        if not link:
            for candidate in node.findall("atom:link", _NS):
                if candidate.get("rel", "alternate") == "alternate" and candidate.get(
                    "href"
                ):
                    link = str(candidate.get("href"))
                    break
        summary = _clean(
            _text(node.find("description"))
            or _text(node.find("rss1:description", _NS))
            or _text(node.find("atom:summary", _NS))
        )
        published = _date(
            _text(node.find("pubDate"))
            or _text(node.find("dc:date", _NS))
            or _text(node.find("atom:published", _NS))
            or _text(node.find("atom:updated", _NS))
        )
        if not title or not link.startswith("https://"):
            continue
        if len(summary) > SUMMARY_CHARS:
            summary = summary[: SUMMARY_CHARS - 1].rsplit(" ", 1)[0] + "…"
        items.append(
            Item(
                title=title[:300],
                outlet=outlet.name,
                language=outlet.language,
                url=link[:600],
                published=published.isoformat(timespec="minutes")
                if published
                else None,
                summary=summary,
                _when=published,
            )
        )
    return items


def outlets(language: str) -> list[Outlet]:
    allowed = {
        part.strip()
        for part in os.environ.get("ATLAS_NEWS_OUTLETS", "").split(",")
        if part.strip()
    }
    chosen = [o for o in OUTLETS if not allowed or o.id in allowed]
    own = [o for o in chosen if o.language == language]
    return own or [o for o in chosen if o.language == "en"]


async def _read(outlet: Outlet, timeout: float) -> list[Item]:
    body = await reference_fetch.get(
        outlet.url,
        capability=CAPABILITY,
        timeout=timeout,
        ttl=FEED_SECONDS,
        max_bytes=MAX_FEED_BYTES,
        extra_hosts=HOSTS,
    )
    return await asyncio.to_thread(parse, body, outlet)


def _key(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(
        (
            parts.scheme,
            (parts.hostname or "").removeprefix("www."),
            parts.path.rstrip("/"),
            "",
            "",
        )
    )


def select(
    items: list[Item], words: list[str], now: datetime, days: int, limit: int
) -> list[Item]:
    stems = [_stem(w) for w in words]
    oldest = now - timedelta(days=days)
    scored: list[tuple[int, datetime, Item]] = []
    for item in items:
        when = item.get("_when")
        if isinstance(when, datetime) and when < oldest:
            continue
        title = plain(str(item["title"]))
        body = plain(str(item.get("summary", "")))
        title_words = set(re.findall(r"[a-z0-9]+", title))
        body_words = set(re.findall(r"[a-z0-9]+", body))
        hits = 0
        score = 0
        for stem in stems:
            in_title = any(w.startswith(stem) for w in title_words)
            in_body = any(w.startswith(stem) for w in body_words)
            if in_title or in_body:
                hits += 1
                score += 2 if in_title else 1
        # Every word for one or two words; all but one beyond.
        needed = len(stems) if len(stems) <= 2 else len(stems) - 1
        if stems and hits < needed:
            continue
        scored.append((score, when if isinstance(when, datetime) else oldest, item))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    chosen: list[Item] = []
    seen_links: set[str] = set()
    seen_titles: set[str] = set()
    per_outlet: dict[str, int] = {}
    cap = max(2, limit // 3) if not stems else limit
    for _, _, item in scored:
        link, title = _key(str(item["url"])), plain(str(item["title"]))[:70]
        outlet = str(item["outlet"])
        if (
            link in seen_links
            or title in seen_titles
            or per_outlet.get(outlet, 0) >= cap
        ):
            continue
        seen_links.add(link)
        seen_titles.add(title)
        per_outlet[outlet] = per_outlet.get(outlet, 0) + 1
        chosen.append(item)
        if len(chosen) >= limit:
            break
    if not stems:
        chosen.sort(key=lambda item: str(item.get("published") or ""), reverse=True)
    return chosen


async def headlines(
    topic: str | None,
    language: str,
    timeout: float,
    *,
    days: int = 3,
    limit: int = 10,
    now: datetime | None = None,
) -> dict[str, object]:
    # Refused as a whole before any feed is read, not as feeds that failed.
    require_shared(CAPABILITY)
    now = now or datetime.now(timezone.utc)
    words = topic_words(topic or "")
    chosen_outlets = outlets(language)
    results = await asyncio.gather(
        *(_read(outlet, timeout) for outlet in chosen_outlets), return_exceptions=True
    )
    items: list[Item] = []
    read, failed = [], []
    for outlet, result in zip(chosen_outlets, results):
        if isinstance(result, list):
            items.extend(result)
            read.append(outlet.name)
        elif isinstance(result, SourceError):
            failed.append({"outlet": outlet.name, "error": result.code})
        elif isinstance(result, BaseException) and not isinstance(result, Exception):
            raise result
        else:
            failed.append({"outlet": outlet.name, "error": "feed_unavailable"})
    if not read:
        raise SourceError(
            "news_feeds_unavailable",
            "no news feed could be read; use web_search for this question",
        )
    chosen = select(items, words, now, days, limit)
    for item in chosen:
        item.pop("_when", None)
    output: dict[str, object] = {
        "source": "public RSS and Atom feeds of the outlets listed",
        "topic_words": words,
        "window_days": days,
        "items": [dict(item) for item in chosen],
        "outlets_read": sorted(set(read)),
        "feed_urls": [o.url for o in chosen_outlets if o.name in read],
        "retrieved_at": now.isoformat(timespec="minutes"),
    }
    if failed:
        output["outlets_failed"] = failed
    if not chosen:
        output["note"] = (
            "No headline of the last days matches these words in these feeds. Say so, "
            "or use web_search for a wider search; never invent a news item."
        )
    else:
        output["note"] = (
            "Headlines and short descriptions only: state the outlet and date of each "
            "item and link it; the article itself was not read."
        )
    return output
