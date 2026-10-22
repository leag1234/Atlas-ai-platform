"""Deterministic useful code module spanning at least 24 KiB, with distant probes."""

import json
from pathlib import Path
from typing import Any


def context_case() -> tuple[dict[str, Any], dict[str, int], int]:
    lines: list[str] = []
    while sum(len(line.encode()) for line in lines) < 24576:
        i = len(lines)
        lines.append(
            f"def normalize_component_{i:03d}(value: int) -> int: return value + {1000 + i}\n"
        )
    expected = {
        f"normalize_component_{i:03d}": 1000 + i
        for i in (0, len(lines) // 2, len(lines) - 1)
    }
    code = "".join(lines)
    request = {
        "model": "atlas-code",
        "max_tokens": 512,
        "messages": [
            {
                "role": "system",
                "content": Path("tests/journeys/dev-context.txt").read_text(),
            },
            {
                "role": "user",
                "content": "Requested: " + ", ".join(expected) + "\n\n" + code,
            },
        ],
    }
    return request, expected, len(code.encode())


def context_answer(text: str) -> dict[str, int]:
    # Some providers wrap requested JSON in a Markdown fence. Values stay exact.
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[8:-4]
    value = json.loads(text)
    if not isinstance(value, dict) or any(type(v) is not int for v in value.values()):
        raise ValueError("invalid_context_answer")
    return value
