"""Search-engine redirect links resolved to their target, or not used at all.

Staging -110 (2026-11-17): a Madrid-Barcelona answer cited four
"https://www.google.es/goto?url=CAES..." links: tracking redirects a search
engine gave instead of the pages. A result link that is a redirect of a search
engine is replaced by the address it carries in clear; a redirect whose target
is an opaque token cannot be shown as a source and the result is not used.
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


def resolved(url: str) -> str | None:
    """``url`` itself, the page a search redirect carries, or None."""
    parts = urlsplit(url)
    host = parts.netloc.lower().split(":")[0]
    if not _ENGINES.search(host) or not _PATHS.match(parts.path or "/"):
        return url
    query = parse_qs(parts.query)
    for key in _TARGETS:
        for value in query.get(key, []):
            target = unquote(value)
            if target.startswith(("https://", "http://")):
                return resolved(target) if target != url else None
    return None
