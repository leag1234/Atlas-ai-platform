"""Seeded language classification; code is excluded and confidence stays at 0.8."""

import importlib
import re


def matches(text: str, language: str) -> bool:
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"<details\b[^>]*>.*?</details>", "", text, flags=re.S | re.I)
    text = re.sub(r"\[[^\]]*\]\(https?://[^)]+\)", "", text)
    text = re.sub(r"https?://\S+", "", text).strip()
    if not re.search(r"[^\W\d_]", text):
        return False
    detector = importlib.import_module("langdetect")
    detector.DetectorFactory.seed = 0
    result = detector.detect_langs(text)[0]
    return bool(result.lang == language and result.prob >= 0.8)
