"""Characters the content font can draw, and the text adapted to them.

Every output format uses one embedded face (Bitstream Vera). A character it
lacks used to refuse the whole document: a report on electric cars that wrote
"CO₂", or an arrow, was never delivered (staging 2026-11-12). Text is now
adapted when it is validated, so every format shows the same characters:
- a known equivalent replaces the character (arrows, check marks, Greek
  letters by name, subscripts and superscripts by their digit, an accented
  Latin letter by its base letter);
- an emoji or a pictograph is dropped, with the space it leaves;
- anything else is kept, and the layout still refuses a script the face does
  not cover (a document written in Chinese, for example).
"""

from functools import lru_cache
from pathlib import Path
import re
import unicodedata

import reportlab
from reportlab.pdfbase.ttfonts import TTFont

FONT_FILE = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"

_EQUIVALENTS = {
    " ": " ",  # narrow no-break space: French typography
    " ": " ",  # thin space
    "→": "->",
    "←": "<-",
    "↔": "<->",
    "⇒": "=>",
    "⇐": "<=",
    "⇔": "<=>",
    "⟶": "->",
    "⟵": "<-",
    "↑": "^",
    "↓": "v",
    "✓": "+",
    "✔": "+",
    "✅": "+",
    "✗": "x",
    "✘": "x",
    "❌": "x",
    "−": "-",
    "‑": "-",
    "‐": "-",
    "∕": "/",
    "≈": "~",
    "≠": "!=",
    "≤": "<=",
    "≥": ">=",
    "∞": "infini",
}
# Pictographs, dingbats and emoji, and the joiners and selectors around them.
_PICTOGRAPH = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿︎️‍⃣]")


@lru_cache(maxsize=1)
def covered() -> frozenset[int]:
    """Code points the content face can draw."""
    return frozenset(TTFont("AtlasGlyphCoverage", str(FONT_FILE)).face.charToGlyph)


def _greek(character: str) -> str | None:
    name = unicodedata.name(character, "")
    for prefix, case in (
        ("GREEK SMALL LETTER ", str.lower),
        ("GREEK CAPITAL LETTER ", str.capitalize),
    ):
        if name.startswith(prefix) and " " not in name[len(prefix) :]:
            return case(name[len(prefix) :])
    return None


def adapt(text: str) -> str:
    """The text with the characters the face lacks replaced or dropped."""
    face = covered()
    if all(ord(character) in face for character in text):
        return text
    result: list[str] = []
    for character in text:
        if ord(character) in face:
            result.append(character)
            continue
        if character in _EQUIVALENTS:
            result.append(_EQUIVALENTS[character])
            continue
        if _PICTOGRAPH.match(character):
            continue
        normal = unicodedata.normalize("NFKC", character)
        if normal != character and all(ord(c) in face for c in normal):
            result.append(normal)
            continue
        # A Latin letter with an accent the face lacks keeps its base letter
        # ("ő" in a Hungarian street name becomes "o").
        base = "".join(
            c
            for c in unicodedata.normalize("NFKD", character)
            if not unicodedata.combining(c)
        )
        if base and base != character and all(ord(c) in face for c in base):
            result.append(base)
            continue
        greek = _greek(character)
        if greek is not None:
            result.append(greek)
            continue
        result.append(character)
    adapted = "".join(result)
    # A dropped pictograph leaves no double space and no leading space.
    adapted = re.sub(r"(?<=\S) {2,}(?=\S)", " ", adapted)
    return adapted.strip(" ") if adapted.strip(" ") else adapted
