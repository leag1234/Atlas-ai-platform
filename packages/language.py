"""Resolve conversation language independently of model output and UI labels."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import re
from collections.abc import Sequence
import unicodedata

from typing import Protocol


class LanguageMessage(Protocol):
    @property
    def role(self) -> str: ...

    @property
    def text(self) -> str: ...


LANGUAGES = ("fr", "de", "es", "it", "en")


def status_language(messages: Sequence[LanguageMessage], locale: object) -> str:
    """Interface text follows a supported UI locale, then conversation prose."""
    if locale == "nl":
        return "en"
    if isinstance(locale, str) and locale in LANGUAGES:
        return locale
    return conversation_language(messages, "en") if messages else "en"


def user_prose(text: str) -> str:
    text = re.sub(r"<attached_files>.*?</attached_files>", "", text, flags=re.S)
    return re.sub(r"^\[Attachment: [^\n]*\]$", "", text, flags=re.M).strip()


def detected_language(text: str) -> str | None:
    text = user_prose(text)
    normalized = "".join(
        c
        for c in unicodedata.normalize("NFKD", text.lower())
        if not unicodedata.combining(c)
    )
    if re.search(
        r"\b(decris|genere|dessine|voudrais|bonjour|francais|resume|synthese)\b",
        normalized,
    ):
        return "fr"
    # Exact-person questions are usually short, so statistical language detection
    # has too little prose to distinguish their common interrogative forms.
    question_starts = (
        ("fr", r"\b(?:qui\s+est|qui\s+etait|parle[ -]?moi\s+de)\b"),
        ("de", r"\b(?:wer\s+ist|wer\s+war)\b"),
        ("es", r"\b(?:quien\s+es|quien\s+fue)\b"),
        ("it", r"\b(?:chi\s+e|chi\s+era)\b"),
        ("en", r"\b(?:who\s+is|who\s+was|tell\s+me\s+about)\b"),
    )
    for language, pattern in question_starts:
        if re.search(pattern, normalized):
            return language
    if re.match(r"\s*i would like\b", normalized):
        return "en"
    if not re.search(r"[^\W\d_]", text):
        return None
    detector = importlib.import_module("langdetect")
    detector.DetectorFactory.seed = 0
    try:
        # Capitalization carries emphasis, not language. The statistical detector
        # misclassifies all-uppercase French instructions as English otherwise.
        scores = detector.detect_langs(text.lower() if text.isupper() else text)
    except detector.lang_detect_exception.LangDetectException:
        return None
    language = str(scores[0].lang)
    return language if scores[0].prob >= 0.8 and language in LANGUAGES else None


def question_language(text: str, fallback: str) -> str:
    return detected_language(text) or fallback


def requested_language(text: str) -> str | None:
    """Resolve explicit output-language instructions, independently of prose locale."""
    normalized = "".join(
        c
        for c in unicodedata.normalize("NFKD", user_prose(text).lower())
        if not unicodedata.combining(c)
    )
    normalized = re.sub(r'`[^`]*`|"[^"]*"|«[^»]*»', "", normalized)
    names = {
        "en": "english|anglais|englisch|ingles|inglese",
        "fr": "french|francais|franzosisch|frances|francese",
        "de": "german|allemand|deutsch(?:e[nmrs]?)?|aleman|tedesco",
        "es": "spanish|espagnol|spanisch|espanol|spagnolo",
        "it": "italian|italien|italienisch|italiano",
    }
    directive = r"\b(?:translat\w*|tradui\w*|tradu[cz]\w*|traduc\w*|tradurr\w*|uberset\w*|answer|reply|respond\w*|repond\w*|antworte\w*|rispondi)\b"
    chosen: str | None = None
    for clause in re.split(r"[.!?;\n]", normalized):
        if re.search(r"\b(?:not|don't|never|pas|ne|nicht|no|non)\b", clause):
            continue
        if not re.search(directive, clause) and not re.match(
            r"^\s*(?:(?:please|bitte)\s+)?(?:in|en|auf)\b", clause
        ):
            continue
        matches = [
            (match.start(), language)
            for language, aliases in names.items()
            for match in re.finditer(
                r"\b(?:into|to|in|en|al|auf|ins|vers le)\s+(?:" + aliases + r")\b",
                clause,
            )
        ]
        if matches:
            chosen = max(matches)[1]
    return chosen


def conversation_language(messages: Sequence[LanguageMessage], locale: str) -> str:
    # Dutch is supported by search; text UI/prompt translations remain unchanged.
    if locale == "nl":
        locale = "en"
    if locale not in LANGUAGES:
        raise ValueError("unsupported_language")
    history: str | None = None
    for message in messages[:-1]:
        if message.role == "user":
            history = detected_language(message.text) or history
    if history is None:
        for message in messages[:-1]:
            if message.role == "assistant":
                history = detected_language(message.text) or history
    current = user_prose(messages[-1].text)
    if len(re.findall(r"\b[\w'-]+\b", current)) < 5:
        if history is not None:
            return history
        if language := detected_language(current):
            return language
        # Statistical detection is unreliable on very short messages. Retain
        # explicit French cues locked by M19, otherwise honor the UI locale.
        normalized = "".join(
            c
            for c in unicodedata.normalize("NFKD", current.lower())
            if not unicodedata.combining(c)
        )
        if re.search(
            r"\b(decris|genere|dessine|voudrais|bonjour|francais|resume|synthese)\b",
            normalized,
        ):
            return "fr"
        return locale
    return question_language(current, history or locale)


def language_instruction(language: str) -> str:
    if language not in LANGUAGES:
        raise ValueError("unsupported_language")
    instructions = json.loads(Path("prompts/language.json").read_text())
    return str(instructions[language])
