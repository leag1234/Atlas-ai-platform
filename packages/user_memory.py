"""P1d personal-memory inputs, explicit intent and bounded context projection."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from packages.context_usage import encoded, tokens

Kind = Literal["preference", "context", "recurring_topic", "fact"]
CONTEXT_TOKENS = 300
MAX_ENTRIES = 100
MAX_TOKENS = 5000


class MemoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=500, pattern=r"\S")
    kind: Kind = "fact"


class MemorySetting(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


def normalized(text: str) -> str:
    return " ".join(
        re.findall(
            r"\w+",
            "".join(
                c
                for c in unicodedata.normalize("NFKD", text.casefold())
                if not unicodedata.combining(c)
            ),
        )
    )


def command(text: str) -> tuple[str, str] | None:
    # Anchored imperative only: quotes, negations and document prose do not write.
    if re.match(
        r"\s*(?:merk(?:e)? dir|vergiss)\s+(?:nicht|nichts)\b|\s*(?:retiens|oublie)\s+pas\b",
        text,
        re.I,
    ):
        return None
    for action, pattern in (
        (
            "save",
            r"remember(?: that)?|retiens(?: que)?|merk(?:e)? dir(?:,? dass)?|recuerda(?: que)?|ricorda(?: che)?",
        ),
        (
            "forget",
            r"forget(?: that)?|oublie(?: que)?|vergiss(?:,? dass)?|olvida(?: que)?|dimentica(?: che)?",
        ),
    ):
        match = re.fullmatch(
            r"\s*(?:" + pattern + r")\s*[:：]?\s+(.+?)\s*", text, re.I | re.S
        )
        if match:
            return action, match[1].strip()
    question = normalized(text)
    if question in {
        "what do you remember about me",
        "what do you know about me",
        "my saved preferences",
        "show my memory",
        "what have you saved about me",
        "que sais tu de moi",
        "quelles informations as tu sur moi",
        "qu as tu retenu de moi",
        "que retiens tu de moi",
        "was weißt du uber mich",
        "was weisst du uber mich",
        "que recuerdas de mi",
        "cosa ricordi di me",
    }:
        return "list", ""
    return None


def inferred_kind(text: str) -> Kind:
    value = normalized(text)
    if re.search(
        r"vegetari|vegan|prefer|allerg|j aime|i like|i love|ich mag|me gusta|mi piace",
        value,
    ):
        return "preference"
    if re.search(
        r"\b(work|travaille|lavoro|trabajo|arbeite|live|habite|wohne)\b", value
    ):
        return "context"
    return "fact"


def projection(entries: list[dict[str, Any]]) -> str:
    return encoded(
        {
            **(
                {
                    "notice": "These are memories the user asked you to keep; you may say you remember them."
                }
                if entries
                else {}
            ),
            "user_memory": [
                {k: e[k] for k in ("id", "kind", "text", "source", "observed_at")}
                for e in entries
            ],
        }
    )


def select(entries: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    terms = set(normalized(query).split())
    selected = [e for e in entries if e["kind"] == "preference"]
    others = [
        (len(terms & set(normalized(e["text"]).split())), e)
        for e in entries
        if e["kind"] != "preference"
    ]
    for score, entry in sorted(others, key=lambda pair: (-pair[0], pair[1]["id"])):
        if score and tokens(projection([*selected, entry])) <= CONTEXT_TOKENS:
            selected.append(entry)
    return selected
