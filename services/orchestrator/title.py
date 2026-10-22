"""Bounded handling for Open WebUI's auxiliary conversation-title request."""

import json
import re

from packages.language import question_language


_TITLE_TASK = "Generate a concise title summarizing the chat history."


def is_title_task(text: str) -> bool:
    """Recognize the pinned Open WebUI title payload, not user prose."""
    return _TITLE_TASK in text and "<chat_history>" in text and '"title"' in text


def _history_question(text: str) -> str:
    history = text.partition("<chat_history>")[2].partition("</chat_history>")[0]
    user_turns = re.findall(r"(?:USER|User|user)\s*:\s*(.+)", history)
    return (user_turns[-1] if user_turns else history).strip()


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W\d_][\w'’-]*", text, flags=re.UNICODE)


def title_response(text: str, locale: str) -> str:
    """Return Open WebUI's required raw JSON response with at most six words."""
    question = _history_question(text)
    language = question_language(question, locale)
    words = _words(question)
    stopwords = {
        "fr": {
            "quel",
            "quelle",
            "quels",
            "quelles",
            "est",
            "sont",
            "pour",
            "avec",
            "dans",
            "une",
            "des",
            "les",
            "sur",
            "peux",
            "tu",
        },
        "en": {
            "what",
            "which",
            "does",
            "this",
            "that",
            "with",
            "about",
            "the",
            "and",
            "for",
            "can",
            "you",
        },
        "de": {
            "was",
            "welche",
            "welcher",
            "welches",
            "mit",
            "und",
            "der",
            "die",
            "das",
            "für",
            "ist",
        },
        "es": {
            "qué",
            "cual",
            "cuál",
            "con",
            "para",
            "los",
            "las",
            "una",
            "del",
            "que",
            "es",
        },
        "it": {
            "quale",
            "quali",
            "con",
            "per",
            "una",
            "del",
            "che",
            "sono",
            "è",
            "gli",
            "delle",
        },
    }
    selected = [word for word in words if word.lower() not in stopwords[language]][:6]
    fallback = {
        "fr": "Nouvelle conversation",
        "en": "New conversation",
        "de": "Neue Unterhaltung",
        "es": "Nueva conversación",
        "it": "Nuova conversazione",
    }
    return json.dumps(
        {"title": " ".join(selected) or fallback[language]}, ensure_ascii=False
    )
