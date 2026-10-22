"""Resolve conversation language independently of model output and UI labels."""

import importlib
import json
from pathlib import Path
import re
from collections.abc import Sequence
import unicodedata

from packages.images import VisionMessage

LANGUAGES = ("fr", "de", "es", "it", "en")


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


def conversation_language(messages: Sequence[VisionMessage], locale: str) -> str:
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
