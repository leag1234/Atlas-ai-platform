"""Typed memory projection and explicit user intent (REQ-PROJ-02/04)."""

from __future__ import annotations

import re
from typing import Any, Literal

from packages.context_usage import encoded, tokens

MemoryKind = Literal[
    "constraint", "decision", "fact", "preference", "open_question", "summary"
]
KINDS = ("constraint", "decision", "fact", "preference", "open_question", "summary")


def normalize(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    if result.get("kind") not in KINDS:
        result["kind"] = "fact"
    result.setdefault("pinned", result["kind"] == "constraint")
    result.setdefault(
        "source", "document" if result.get("source_file_version_ids") else "user"
    )
    result.setdefault("observed_at", result["created_at"])
    result.setdefault("supersedes", None)
    result.setdefault("superseded_by", None)
    return result


def pinned(value: dict[str, Any]) -> bool:
    return value["kind"] == "constraint" or bool(value.get("pinned"))


def pin_tokens(entries: list[dict[str, Any]]) -> int:
    return tokens(encoded({"pinned_memory": entries}))


# The fact runs to its last non-space character: "(.*\S|.)" is the former lazy
# "(.+?)" before trailing spaces, read once instead of retried at each
# character (a long space run was cubic).
EXPLICIT = re.compile(
    r"\s*+(?:remember that|remember|retiens que|retiens|merke dir(?:,? dass)?|recuerda que|recuerda|ricorda che|ricorda)\s*[:：]?\s+(.*\S|.)\s*",
    re.IGNORECASE | re.DOTALL,
)


def explicit(text: str) -> str | None:
    match = EXPLICIT.fullmatch(text)
    if match:
        fact = re.sub(
            r"^(?:"
            r"this fact(?: for (?:this|the) project)?|this for (?:this|the) project"
            r"|ce fait(?: pour (?:ce|le) projet)?|(?:ceci|cela|ça) pour (?:ce|le) projet"
            r"|diese Tatsache(?: f(?:ü|ue)r (?:dieses|das) Projekt)?|(?:dies|das) f(?:ü|ue)r (?:dieses|das) Projekt"
            r"|este (?:hecho|dato)(?: para (?:este|el) proyecto)?|esto para (?:este|el) proyecto"
            r"|questo fatto(?: per (?:questo|il) progetto)?|questo per (?:questo|il) progetto"
            r")\s*[:：]\s*",
            "",
            match[1],
            count=1,
            flags=re.IGNORECASE,
        )
        if fact.strip() and 0 < len(fact) <= 500:
            return fact
    return None
