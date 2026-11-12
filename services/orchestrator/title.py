"""Bounded handling for Open WebUI's auxiliary conversation-title request.

The title is written by the fast profile in one short call (ATLAS_MODEL_TITLES=1);
on any failure, or with the flag off, a deterministic title is built from the
question itself. Either way the answer is the raw JSON Open WebUI expects.
"""

import asyncio
import json
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from packages.language import question_language


_TITLE_TASK = "Generate a concise title summarizing the chat history."
_MAX_WORDS = 7
_MAX_CHARS = 60
_FALLBACK = {
    "fr": "Nouvelle conversation",
    "en": "New conversation",
    "de": "Neue Unterhaltung",
    "es": "Nueva conversación",
    "it": "Nuova conversazione",
}
_LANGUAGE_NAMES = {
    "fr": "French",
    "en": "English",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
}
# Polite request openings that carry no subject ("Can you ...", "Pouvez-vous ...").
_OPENING = re.compile(
    r"^(?:(?:hi|hello|hey|bonjour|salut|hallo|hola|ciao)\b[\s,!.]*)?"
    r"(?:(?:please|stp|svp|s'il te plaît|s'il vous plaît|bitte|por favor|per favore)\b[\s,]*)?"
    r"(?:(?:can|could|would) you(?: please)?|pouvez-vous|pourriez-vous|peux-tu|"
    r"pourrais-tu|est-ce que tu peux|est-ce que vous pouvez|kannst du|können sie|"
    r"könnten sie|puedes|podrías|puede|puoi|potresti|può)\b[\s,]*",
    re.IGNORECASE,
)

Complete = Callable[[dict[str, Any], float], Awaitable[dict[str, Any]]]


def is_title_task(text: str) -> bool:
    """Recognize the pinned Open WebUI title payload, not user prose."""
    return _TITLE_TASK in text and "<chat_history>" in text and '"title"' in text


def _history_question(text: str) -> str:
    history = text.partition("<chat_history>")[2].partition("</chat_history>")[0]
    user_turns = re.findall(r"(?:USER|User|user)\s*:\s*(.+)", history)
    return (user_turns[0] if user_turns else history).strip()


def _language(question: str, locale: str) -> str:
    language = question_language(question, locale)
    return language if language in _FALLBACK else "en"


def _sentence_case(title: str) -> str:
    return title[:1].upper() + title[1:] if title else title


def heuristic_title(question: str, language: str) -> str:
    """The first words of the question, without a polite opening, numbers kept."""
    first = re.split(r"(?<=[.!?])\s|\n", question.strip(), maxsplit=1)[0]
    first = re.sub(r"`[^`]*`|https?://\S+", " ", first)
    first = _OPENING.sub("", first.strip())
    words = [
        word.strip(".,;:!?\"'«»()[]{}")
        for word in first.split()
        if word.strip(".,;:!?\"'«»()[]{}")
    ][:_MAX_WORDS]
    title = " ".join(words)
    while len(title) > _MAX_CHARS and " " in title:
        title = title.rsplit(" ", 1)[0]
    return _sentence_case(title[:_MAX_CHARS]) or _FALLBACK.get(
        language, _FALLBACK["en"]
    )


def valid_title(value: object) -> str | None:
    """A short single-line title, or None."""
    if not isinstance(value, str):
        return None
    title = value.strip().strip("\"'«»“”").rstrip(".").strip()
    if not title or "\n" in title or len(title) > _MAX_CHARS:
        return None
    if len(title.split()) > _MAX_WORDS + 1:
        return None
    return _sentence_case(title)


def enabled() -> bool:
    return os.environ.get("ATLAS_MODEL_TITLES") == "1"


def _payload(question: str, language: str) -> dict[str, Any]:
    from services.orchestrator import request_router

    name = _LANGUAGE_NAMES.get(language, "English")
    return dict(
        messages=[
            {
                "role": "system",
                "content": (
                    "You name chat conversations. Reply with JSON only: "
                    '{"title": "..."}. The title has 2 to 6 words, names the '
                    "subject of the user's first message, is written in "
                    f"{name}, in sentence case, without quotes, emoji or final "
                    "punctuation. Never answer the message itself."
                ),
            },
            {"role": "user", "content": question[:600]},
        ],
        tools=[],
        response_format={"type": "json_object"},
        profile=request_router.profile(),
        max_tokens=40,
        timeout=4,
        budget_eur="0.005",
        local_enabled=False,
    )


async def model_title(
    question: str, language: str, *, complete: Complete | None = None
) -> tuple[str | None, Decimal]:
    """One bounded call to the fast profile: (title or None, cost)."""
    from services.orchestrator import request_router

    cost = Decimal(0)
    try:
        async with asyncio.timeout(4):
            result = await (complete or request_router._gateway)(
                _payload(question, language), 4
            )
        try:
            cost = Decimal(str(result.get("cost_eur") or 0))
        except InvalidOperation:
            cost = Decimal(0)
        if result.get("calls"):
            return None, cost
        text = str(result.get("text") or "")
        text = text[text.find("{") : text.rfind("}") + 1]
        return valid_title(json.loads(text).get("title")), cost
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - any failure falls back to the question's words
        return None, cost


async def title_answer(
    text: str, locale: str, *, complete: Complete | None = None
) -> tuple[str, Decimal]:
    """Open WebUI's required raw JSON title answer, and the cost of the call."""
    question = _history_question(text)
    language = _language(question, locale)
    title, cost = (None, Decimal(0))
    if enabled() and question:
        title, cost = await model_title(question, language, complete=complete)
    return (
        json.dumps(
            {"title": title or heuristic_title(question, language)},
            ensure_ascii=False,
        ),
        cost,
    )


def title_response(text: str, locale: str) -> str:
    """Deterministic title answer (no model call)."""
    question = _history_question(text)
    return json.dumps(
        {"title": heuristic_title(question, _language(question, locale))},
        ensure_ascii=False,
    )
