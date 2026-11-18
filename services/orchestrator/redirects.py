"""Search-engine redirect links resolved to their target, or not used at all.

Staging -110 (2026-11-17): a Madrid-Barcelona answer cited four
"https://www.google.es/goto?url=CAES..." links: tracking redirects a search
engine gave instead of the pages. A result link that is a redirect of a search
engine is replaced by the address it carries in clear; a redirect whose target
is an opaque token cannot be shown as a source and the result is not used.

Staging -112 (2026-11-17): the same redirects still reached Sydney and Madrid
answers through another search provider, cited as "(google.co.uk)" and
"(fuente)". Every provider's results are cleaned here (``results``), and a
link an answer cites is shown by the domain it leads to, or not at all
(``cited``). A search engine's own result pages (a search, a travel or flight
listing) are no source either.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlsplit

# Hosts of search engines and their redirect paths (not places, not sites).
_ENGINES = re.compile(
    r"(?:^|\.)(?:google|bing|duckduckgo|yahoo|yandex|startpage|qwant|ecosia|"
    r"mojeek|brave|seznam|baidu|naver)\.[a-z.]{2,12}$"
)
_PATHS = re.compile(r"^/(?:url|goto|l|link|ck/a|aclk|rd|r|redirect|do/redirect)/?$")
_TARGETS = ("url", "q", "u", "uddg", "target", "dest", "to", "adurl")
# A search engine's generated listings: what it found, not a page about it.
_LISTINGS = re.compile(
    r"^/(?:search|webhp|travel|flights|hotels|maps/search|maps/dir|shopping|news)(?:/|$)"
)


def resolved(url: str) -> str | None:
    """``url`` itself, the page a search redirect carries, or None."""
    parts = urlsplit(url)
    host = parts.netloc.lower().split(":")[0]
    if not _ENGINES.search(host):
        return url
    if _LISTINGS.match(parts.path or "/") and not _PATHS.match(parts.path or "/"):
        return None
    if not _PATHS.match(parts.path or "/"):
        return url
    query = parse_qs(parts.query)
    for key in _TARGETS:
        for value in query.get(key, []):
            target = unquote(value)
            if target.startswith(("https://", "http://")):
                return resolved(target) if target != url else None
    return None


def results(result: dict[str, object]) -> dict[str, object]:
    """A search result whose rows link to pages: redirects resolved, the rest
    dropped, one row per page."""
    rows = result.get("results")
    if not isinstance(rows, list):
        return result
    kept: list[object] = []
    seen: set[str] = set()
    changed = False
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("link"), str):
            kept.append(row)
            continue
        link = resolved(row["link"])
        if link is None or link in seen:
            changed = True
            continue
        seen.add(link)
        if link != row["link"]:
            changed = True
            row = {**row, "link": link}
        kept.append(row)
    # Forum and social posts after the pages of operators, references and
    # publishers (staging -114: a Reddit thread was the source of a train's
    # running days).
    ranked = sorted(
        kept,
        key=lambda row: isinstance(row, dict)
        and isinstance(row.get("link"), str)
        and forum(row["link"]),
    )
    if ranked != kept:
        kept, changed = ranked, True
    return {**result, "results": kept} if changed else result


def site(url: str) -> str:
    return (
        urlsplit(url).netloc.lower().split("@")[-1].split(":")[0].removeprefix("www.")
    )


_CITED = re.compile(
    r"[ \t]*\(\[([^\]\n]+)\]\((https?://[^)\s]+)\)\)|\[([^\]\n]+)\]\((https?://[^)\s]+)\)"
)
# A label that only says "a source": shown as the site the link leads to.
_GENERIC_LABEL = re.compile(
    r"(?i)^(?:source|sources|link|lien|page|fuente|fuentes|enlace|quelle|"
    r"quellen|fonte|fonti|\d{1,2})$"
)
_DOMAIN = re.compile(r"(?i)^[a-z0-9-]++(?:\.[a-z0-9-]++)++$")


def cited(text: str) -> str:
    """Links of an answer: a search redirect shows the page it leads to, by its
    domain; one leading nowhere is not shown (its words stay when they are
    prose); a label that only says "source" names the site."""

    def fix(match: re.Match[str]) -> str:
        wrapped = match[1] is not None
        label = match[1] if wrapped else match[3]
        url = match[2] if wrapped else match[4]
        link = resolved(url)
        generic = bool(_GENERIC_LABEL.match(label.strip()))
        if link is None:
            if wrapped or generic or _DOMAIN.match(label):
                return ""
            return label
        if link == url and not generic:
            return match[0]
        shown = site(link) if generic or _DOMAIN.match(label) else label
        lead = match[0][: len(match[0]) - len(match[0].lstrip(" \t"))]
        return f"{lead}([{shown}]({link}))" if wrapped else f"[{shown}]({link})"

    return _CITED.sub(fix, text)


# Forums and social networks: people's posts, not an operator's, a
# reference's or a publisher's page. Never the source of a trip's facts.
_SOCIAL = re.compile(
    r"(?:^|\.)(?:reddit\.com|redd\.it|quora\.com|facebook\.com|fb\.com|"
    r"instagram\.com|tiktok\.com|x\.com|twitter\.com|threads\.net|"
    r"threads\.com|pinterest\.[a-z.]{2,6}|snapchat\.com|linkedin\.com|"
    r"youtube\.com|youtu\.be|discord\.com|telegram\.me|t\.me|vk\.com|"
    r"stackexchange\.com|boards\.ie|flyertalk\.com)$"
)
_FORUM_HOST = re.compile(r"^(?:forums?|community|communaute|boards?|foro|foren)\.")
_FORUM_PATH = re.compile(
    r"(?i)/(?:forums?|foros?|foren|communaute|community|threads?|"
    r"showtopic|viewtopic|topic|discussion|discussions)(?:[/._?-]|$)"
)


def forum(url: str) -> bool:
    """A forum thread or a social network post."""
    parts = urlsplit(url)
    host = parts.netloc.lower().split("@")[-1].split(":")[0]
    return bool(
        _SOCIAL.search(host)
        or _FORUM_HOST.match(host)
        or _FORUM_PATH.search(parts.path)
    )


def without_forums(value: object) -> object:
    """A tool output without its rows from forums or social networks."""
    if isinstance(value, dict):
        link = value.get("link") or value.get("url")
        if isinstance(link, str) and forum(link):
            return {}
        rows = value.get("results")
        if isinstance(rows, list):
            return {**value, "results": [without_forums(row) for row in rows]}
        return value
    if isinstance(value, list):
        return [without_forums(row) for row in value]
    return value


_ANY = re.compile(r"\[([^\]\n]++)\]\((https?://[^)\s]++)\)")


def _unforum(match: re.Match[str]) -> str:
    url = match[2] if match[1] is not None else match[4]
    if not forum(url):
        return match[0]
    return "" if match[1] is not None else match[3]


def forum_claims(text: str) -> str:
    """Sentences of an answer whose only sources are forum posts go; a forum
    citation next to another source goes alone."""
    lines: list[str] = []
    for line in text.split("\n"):
        if "](" not in line or not any(forum(m[2]) for m in _ANY.finditer(line)):
            lines.append(line)
            continue
        kept: list[str] = []
        for sentence in re.split(r"(?<=[.!?])[ \t]++", line):
            links = [m[2] for m in _ANY.finditer(sentence)]
            if links and all(forum(url) for url in links):
                continue
            kept.append(_CITED.sub(_unforum, sentence))
        line = " ".join(kept)
        if re.sub(r"^[ \t]*+(?:[-*+]|\d{1,3}+[.)])?+", "", line).strip():
            lines.append(line)
    return "\n".join(lines)
