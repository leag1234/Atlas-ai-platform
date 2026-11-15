"""Render scoped citations once, using only the turn's authorized passages."""

import re
from typing import Any

from packages import linear_scan
from services.retrieval.project_identity import ProjectError

HASH = r"[a-f0-9]{64}"
# Providers emit hashes, [hash], or [hash](hash). Consume the whole citation
# before substituting, so neither labels nor destinations are rewritten twice.
REFERENCE = re.compile(
    rf"\[({HASH})\]\(https?://[^\s)]+\)"
    rf"|\(({HASH})\)\(https?://[^\s)]+\)"
    # "(?<!\[)": in a run of "[", only the first can start this label; the
    # others reach the same "]" and rescanning from each is quadratic.
    rf"|(?<!\[)\[[^\]\n]*\]\((?:/api/atlas/orgs/[^\s)]+/sources/)?({HASH})\)"
    rf"|\(({HASH})\)\(({HASH})\)|\[({HASH})\]|\b({HASH})\b"
)
_HASH_AT = re.compile(HASH)
_LABEL_STOP = re.compile(r"[\]\n]")


def _reference_viable(text: str) -> linear_scan.Viable:
    """Whether an opening can start a citation, by lookups instead of attempts:
    a "[" whose label run fails was otherwise rescanned from every later "["
    of that run (quadratic). The attempt itself still decides."""
    label_end = linear_scan.RunEnd(text, _LABEL_STOP)

    def hash_at(position: int, closer: str) -> bool:
        return (
            _HASH_AT.match(text, position) is not None
            and text[position + 64 : position + 65] == closer
        )

    def viable(found: re.Match[str]) -> bool:
        start = found.start()
        if text[start] == "(":
            return hash_at(start + 1, ")") and text[start + 66 : start + 67] == "("
        if text[start] != "[":
            return True
        if hash_at(start + 1, "]"):
            return True
        close = label_end(start + 1)
        target = close + 2
        return text[close : close + 2] == "](" and (
            hash_at(target, ")") or text.startswith("/api/atlas/orgs/", target)
        )

    return viable


REFERENCES = linear_scan.Scan(
    REFERENCE,
    re.compile(rf"[\[(]|\b(?={HASH}\b)"),
    linear_scan.attempted,
    _reference_viable,
)


def render(answer: str, passages: list[dict[str, Any]], org: str, project: str) -> str:
    authorized = {p["chunk_id"] for p in passages}
    numbers: dict[str, int] = {}

    def replace(match: re.Match[str]) -> str:
        chunk = next(value for value in match.groups() if value is not None)
        # Validate every hash, including a link label, against current context.
        if any(key not in authorized for key in re.findall(HASH, match[0])):
            raise ProjectError(422, "invalid_request", "Unverified project citation")
        number = numbers.setdefault(chunk, len(numbers) + 1)
        return f"[{number}](/api/atlas/orgs/{org}/projects/{project}/sources/{chunk})"

    # Every form holds a 64-hex hash: without one nothing can match, and the
    # label form would rescan each unclosed "[" to the end of its line.
    if not re.search(HASH, answer):
        return answer
    return REFERENCES.sub(replace, answer)
