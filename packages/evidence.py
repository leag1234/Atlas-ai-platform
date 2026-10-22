"""Whole-chunk token budgeting; estimates never authorize a paid model call."""

import json
import math
import re


def estimated_tokens(text: str) -> int:
    """A transparent estimate; the gateway separately reserves conservative usage."""
    return sum(
        max(1, math.ceil(len(part.encode()) / 4))
        for part in re.findall(r"\w+|[^\w\s]", text, re.UNICODE)
    )


def whole_chunks(
    chunks: list[dict[str, str]], token_budget: int
) -> list[dict[str, str]]:
    if token_budget <= 0:
        raise ValueError("invalid_evidence_budget")
    selected: list[dict[str, str]] = []
    used = 0
    for chunk in chunks:
        size = estimated_tokens(json.dumps(chunk, ensure_ascii=False))
        if used + size <= token_budget:
            selected.append(chunk)
            used += size
    return selected
