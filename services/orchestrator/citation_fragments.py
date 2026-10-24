"""Normalize tool-result citation text before URL provenance filtering."""

import re
from urllib.parse import urlsplit


_CODE = re.compile(r"(```[^\n]*\n[\s\S]*?(?:```|$)|`[^`\n]+`)")
_DATE = r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?"
_VISIT = re.compile(r"(?:,?[ \t]*)consulted_at[ \t]*=[ \t]*" + _DATE)
_URL = r"https?://[^\s<>\"'()\],]+"
_BARE = r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+/[^\s<>\"'()\],]*"
_REFERENCE = re.compile(r"\[\[atlas-reference:([^\]]+)\]\]")


def tool_citations(text: str, retrieved: set[str]) -> str:
    """Recover only unambiguous retrieved aliases; never guess a missing host."""
    aliases: dict[str, set[str]] = {}
    for url in retrieved:
        parts = urlsplit(url)
        if parts.scheme not in {"https", "http"} or not parts.netloc:
            continue
        bare = url.split("://", 1)[1]
        # Typed reference frames can consume the start of the host, or the host
        # and parent path. Retain distinct targets so a shared slug is not proof.
        starts = (
            {0}
            | {m.start() for m in re.finditer(r"[-.]", parts.netloc)}
            | {m.end() for m in re.finditer(r"[./]", bare)}
        )
        for start in starts:
            alias = bare[start:]
            if len(alias) >= 12 and "/" in alias:
                aliases.setdefault(alias, set()).add(url)
    alternatives = "|".join(
        re.escape(a) for a in sorted(aliases, key=len, reverse=True)
    )
    pattern = re.compile(
        r"(?P<url>"
        + _URL
        + r")|(?P<alias>"
        + (alternatives or r"(?!)")
        + r")(?![\w/?#%=-])|(?P<unknown>"
        + _BARE
        + r")"
    )

    def prose(value: str) -> str:
        # Do this before URL filtering: otherwise an unknown URL inside the
        # internal marker becomes nested brackets and no longer parses.
        value = _REFERENCE.sub(
            lambda m: m[0] if m[1].isdecimal() or m[1] in retrieved else "",
            value,
        )
        value = _VISIT.sub("", value)

        def replace(match: re.Match[str]) -> str:
            if match.lastgroup == "url":
                return match[0]
            raw = match[0].rstrip(".,;:")
            tail = match[0][len(raw) :]
            # A glued year or word can look like part of an unknown hostname.
            # Prefer the known suffix without consuming its preceding prose.
            alias = next(
                (a for a in sorted(aliases, key=len, reverse=True) if raw.endswith(a)),
                raw,
            )
            before = raw[: -len(alias)] if alias else ""
            targets = aliases.get(alias, set())
            if len(targets) != 1:
                return tail
            url = next(iter(targets))
            prefix = (
                " "
                if before
                or (
                    match.start()
                    and not value[match.start() - 1].isspace()
                    and value[match.start() - 1] not in "([<"
                )
                else ""
            )
            return before + prefix + url + tail

        return pattern.sub(replace, value)

    return "".join(
        part if index % 2 else prose(part)
        for index, part in enumerate(_CODE.split(text))
    )
