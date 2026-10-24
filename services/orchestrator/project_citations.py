"""Render scoped citations once, using only the turn's authorized passages."""

import re
from typing import Any

from services.retrieval.project_identity import ProjectError

HASH = r"[a-f0-9]{64}"
# Providers emit hashes, [hash], or [hash](hash). Consume the whole citation
# before substituting, so neither labels nor destinations are rewritten twice.
REFERENCE = re.compile(
    rf"\[({HASH})\]\(https?://[^\s)]+\)"
    rf"|\(({HASH})\)\(https?://[^\s)]+\)"
    rf"|\[[^\]\n]*\]\((?:/api/atlas/orgs/[^\s)]+/sources/)?({HASH})\)"
    rf"|\(({HASH})\)\(({HASH})\)|\[({HASH})\]|\b({HASH})\b"
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

    return REFERENCE.sub(replace, answer)
