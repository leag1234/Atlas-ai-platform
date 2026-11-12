"""Characters the content font can draw, and the text adapted to them.

Every output format uses one embedded face (Bitstream Vera). A character it
lacks used to refuse the whole document: a report on electric cars that wrote
"CO₂", or an arrow, was never delivered (staging 2026-11-12). Text is now
adapted when it is validated, so every format shows the same characters:
- a known equivalent replaces the character (arrows, check marks, Greek
  letters by name, subscripts and superscripts by their digit, an accented
  Latin letter by its base letter);
- an arrow becomes ->, <-, ^ or v by its direction, a square, triangle or
  circle used as a bullet becomes a dash;
- an emoji, a pictograph or any other symbol or mark is dropped, with the
  space it leaves;
- only letters are kept, and the layout still refuses a script the face does
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
    "\u2044": "/",  # fraction slash
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


def _arrow(character: str) -> str | None:
    """An arrow by its direction, whatever its style (⇨, ➜, ⟹)."""
    name = unicodedata.name(character, "")
    if "ARROW" not in name and "ARROWHEAD" not in name:
        return None
    if "LEFT RIGHT" in name:
        return "<->"
    for word, text in (
        ("RIGHTWARDS", "->"),
        ("LEFTWARDS", "<-"),
        ("UPWARDS", "^"),
        ("DOWNWARDS", "v"),
        ("RIGHT", "->"),
        ("LEFT", "<-"),
    ):
        if word in name:
            return text
    return "->"


def _character(character: str, face: frozenset[int]) -> str:
    """One character the face can draw, an equivalent, or nothing."""
    if ord(character) in face:
        return character
    if character in _EQUIVALENTS:
        return _EQUIVALENTS[character]
    arrow = _arrow(character)
    if arrow is not None:
        return arrow
    if _PICTOGRAPH.match(character):
        return ""
    normal = unicodedata.normalize("NFKC", character)
    if normal != character:
        # "⅓" is "1⁄3": its fraction slash is adapted too.
        adapted = "".join(
            c if ord(c) in face else _EQUIVALENTS.get(c, "") for c in normal
        )
        if adapted and all(ord(c) in face for c in adapted):
            return adapted
    # A Latin letter with an accent the face lacks keeps its base letter
    # ("ő" in a Hungarian street name becomes "o").
    base = "".join(
        c
        for c in unicodedata.normalize("NFKD", character)
        if not unicodedata.combining(c)
    )
    if base and base != character and all(ord(c) in face for c in base):
        return base
    greek = _greek(character)
    if greek is not None:
        return greek
    category = unicodedata.category(character)
    if category in {"So", "Sk"} and 0x2580 <= ord(character) <= 0x25FF:
        return "-"  # a square, triangle or circle used as a bullet
    if category[0] == "Z":
        return " "  # an em space or another space the face lacks
    if category[0] in {"S", "P", "C", "M"}:
        # Any other symbol or mark the face lacks is dropped; only letters of a
        # script the face does not cover are left for the layout to refuse.
        return ""
    return character


def adapt(text: str) -> str:
    """The text with the characters the face lacks replaced or dropped."""
    face = covered()
    if all(ord(character) in face for character in text):
        return text
    adapted = "".join(_character(character, face) for character in text)
    # A dropped pictograph leaves no double space and no leading space.
    adapted = re.sub(r"(?<=\S) {2,}(?=\S)", " ", adapted)
    return adapted.strip(" ") if adapted.strip(" ") else adapted
