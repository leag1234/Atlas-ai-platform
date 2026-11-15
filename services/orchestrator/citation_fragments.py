"""Normalize tool-result citation text before URL provenance filtering."""

from bisect import bisect_right
import re
from urllib.parse import urlsplit

from packages import linear_scan


# Fenced or inline code. Reference form: code_spans and split_code find the same
# spans in linear time (the expression retries each "```" of a last line without
# a newline up to the end of the answer: 30 000 of them took over 2 seconds).
_CODE = re.compile(r"(```[^\n]*\n[\s\S]*?(?:```|$)|`[^`\n]+`)")
_TICK_OR_NEWLINE = re.compile(r"[`\n]")


def code_spans(text: str) -> list[tuple[int, int]]:
    """Spans of _CODE.finditer(text), found in one pass."""
    spans: list[tuple[int, int]] = []
    last_newline = text.rfind("\n")
    stops = [m.start() for m in _TICK_OR_NEWLINE.finditer(text)]
    position = 0
    while (tick := text.find("`", position)) >= 0:
        # A fence needs a newline after its opening line; it ends at the next
        # "```" or at "$" (the end, or just before a final newline).
        if text.startswith("```", tick) and last_newline > tick + 2:
            body = text.index("\n", tick + 3) + 1
            closing = text.find("```", body)
            anchor = len(text)
            if text.endswith("\n") and len(text) - 1 >= body:
                anchor = len(text) - 1
            end = closing + 3 if 0 <= closing < anchor else anchor
            spans.append((tick, end))
            position = end
            continue
        stop = bisect_right(stops, tick)
        if (
            tick + 1 < len(text)
            and text[tick + 1] not in "`\n"
            and stop < len(stops)
            and text[stops[stop]] == "`"
        ):
            spans.append((tick, stops[stop] + 1))
            position = stops[stop] + 1
            continue
        position = tick + 1
    return spans


def split_code(text: str) -> list[str]:
    """_CODE.split(text): prose and code alternate, code at odd indexes."""
    parts: list[str] = []
    position = 0
    for start, end in code_spans(text):
        parts += [text[position:start], text[start:end]]
        position = end
    parts.append(text[position:])
    return parts


_DATE = r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?"
# A space run is entered at its first character only: a retry from each later
# space rescans the run (the square of its length). The matches are the same.
_VISIT = re.compile(r"(?!(?<=[ \t])[ \t])(?:,?[ \t]*)consulted_at[ \t]*=[ \t]*" + _DATE)
_URL = r"https?://[^\s<>\"'()\],]+"
# At most 32 dotted labels (real host names have a few): each label start of a
# long dotted run otherwise rescans the whole run (25 s on "a.a.a…", 2026-11-15).
_BARE = r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+){1,32}/[^\s<>\"'()\],]*"
_REFERENCE = re.compile(r"\[\[atlas-reference:([^\]]+)\]\]")
# Provider reference IDs can stop at the hostname's first dot. Keep the suffix
# attached to that identity before the legacy bare-URL safety net sees it.
_HOST = r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?:\d{4}-?\d{2}-?\d{2})?"
_CITATION_TAIL = r"(?:/[^\s<>\"'()\[\],;]*)?"
_REFERENCE_TOKEN = (
    _REFERENCE.pattern + r"(?:\.[A-Za-z][A-Za-z0-9.-]*" + _CITATION_TAIL + r")?"
)
_CITATION_RUN = re.compile(
    _REFERENCE_TOKEN
    + r"(?:[,;][ \t]*(?:"
    + _REFERENCE_TOKEN
    + r"|"
    + _HOST
    + _CITATION_TAIL
    + r"))*(?P<date>[,;][ \t]*"
    + _DATE
    + r")?"
)
_CITATION_PART = re.compile(_REFERENCE_TOKEN + r"|" + _HOST + _CITATION_TAIL)
# A run starts with a reference read to its first "]": a failed opening is
# shared by every later opening before that "]", which is not retried.
_REFERENCE_OPENING = re.compile(r"\[\[atlas-reference:")
_CITATION_RUNS = linear_scan.Scan(
    _CITATION_RUN, _REFERENCE_OPENING, linear_scan.up_to(r"\]")
)


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
        # A bare host starts where its name starts, never inside it: that start
        # already reached the same end (each retry rescanned the rest of the
        # token, over 30 s on a 100 000-digit run, 2026-11-15).
        + r"(?<![A-Za-z0-9-])"
        + _BARE
        + r")"
    )

    def prose(value: str) -> str:
        def reference_run(match: re.Match[str]) -> str:
            resolved: list[str] = []
            punctuation = ""
            citation_host = False
            for part in _CITATION_PART.finditer(match[0]):
                raw = part[0].rstrip(".,;:")
                punctuation = part[0][len(raw) :]
                reference = _REFERENCE.match(raw)
                if reference:
                    identifier = reference[1]
                    suffix = raw[reference.end() :]
                    if identifier.isdecimal() or identifier in retrieved:
                        resolved.append(reference[0] + suffix)
                        continue
                    if not suffix:
                        continue
                    raw = identifier + suffix
                citation_host = True
                targets = aliases.get(raw, set())
                if len(targets) == 1:
                    resolved.append(next(iter(targets)))
            # Separators belong to the reference list, not the surrounding prose.
            result = ", ".join(resolved)
            if (
                result.startswith(("http://", "https://"))
                and match.start()
                and not value[match.start() - 1].isspace()
            ):
                result = " " + result
            # A date after hostname annotations belongs to that citation list.
            # Keep ordinary dates following a resolved reference in answer prose.
            date = "" if citation_host else (match["date"] or "")
            return result + punctuation + date

        value = _CITATION_RUNS.sub(reference_run, value)
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
        for index, part in enumerate(split_code(text))
    )
