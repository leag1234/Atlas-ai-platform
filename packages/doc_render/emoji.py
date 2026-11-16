"""Emoji drawn as pictures in the formats laid out by the renderer.

The content face (Bitstream Vera) has no emoji. Until 2026-11-16 every emoji
was dropped while the text was validated, silently: a picture book asked for
"one emoji per word" was delivered as a list of words, and the answer said the
emoji were there (staging 2026-11-16). An emoji is now drawn from the system's
colour emoji font (Noto Color Emoji, SIL Open Font License 1.1, Debian package
fonts-noto-color-emoji): a PDF page gets it as a small picture at the size of
the text, and the Office formats keep the character, which their viewers draw
with their own emoji font. Without that font an emoji is still left out, and
glyphs.left_out names it so that the delivery says so.

Pillow draws the font's colour bitmaps (CBDT) at their own strike of 109
pixels; with its complex text layout (raqm) a sequence joined by U+200D is one
picture (a family, a profession with a skin tone).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import io
import os
from pathlib import Path
import struct

# Debian and Ubuntu (fonts-noto-color-emoji), then Fedora and Arch.
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/google-noto-color-emoji/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
)
# The only bitmap strike of Noto Color Emoji.
STRIKE = 109
ZWJ = "‍"
# Characters that only qualify the emoji before them: presentation selectors,
# the keycap, skin tones and the tag characters of subdivision flags.
_MODIFIERS = frozenset(
    ["︎", "️", "⃣"]
    + [chr(c) for c in range(0x1F3FB, 0x1F400)]
    + [chr(c) for c in range(0xE0020, 0xE0080)]
)
# Characters of the first plane drawn as emoji by default (Unicode
# emoji-data.txt, Emoji_Presentation=Yes, below U+10000). Any other symbol of
# that plane is an emoji only when U+FE0F follows it: an arrow or a check mark
# in a report stays text.
_PRESENTATION = frozenset(
    chr(c)
    for start, end in (
        (0x231A, 0x231B),
        (0x23E9, 0x23EC),
        (0x23F0, 0x23F0),
        (0x23F3, 0x23F3),
        (0x25FD, 0x25FE),
        (0x2614, 0x2615),
        (0x2648, 0x2653),
        (0x267F, 0x267F),
        (0x2693, 0x2693),
        (0x26A1, 0x26A1),
        (0x26AA, 0x26AB),
        (0x26BD, 0x26BE),
        (0x26C4, 0x26C5),
        (0x26CE, 0x26CE),
        (0x26D4, 0x26D4),
        (0x26EA, 0x26EA),
        (0x26F2, 0x26F3),
        (0x26F5, 0x26F5),
        (0x26FA, 0x26FA),
        (0x26FD, 0x26FD),
        (0x2705, 0x2705),
        (0x270A, 0x270B),
        (0x2728, 0x2728),
        (0x274C, 0x274C),
        (0x274E, 0x274E),
        (0x2753, 0x2755),
        (0x2757, 0x2757),
        (0x2795, 0x2797),
        (0x27B0, 0x27B0),
        (0x27BF, 0x27BF),
        (0x2B1B, 0x2B1C),
        (0x2B50, 0x2B50),
        (0x2B55, 0x2B55),
    )
    for c in range(start, end + 1)
)


def _regional(character: str) -> bool:
    return 0x1F1E6 <= ord(character) <= 0x1F1FF


def _starts(character: str, following: str) -> bool:
    """Whether an emoji starts at this character."""
    code = ord(character)
    if 0x1F000 <= code <= 0x1FAFF:
        return code < 0x1F3FB or code > 0x1F3FF
    if character in _PRESENTATION:
        return True
    # A symbol asked as an emoji with U+FE0F: "⚠️", "❤️", "☀️".
    return following == "️" and (0x2000 <= code <= 0x2BFF or character in "©®〰〽㊗㊙")


def tokens(text: str) -> list[str]:
    """The text cut into single characters and whole emoji sequences."""
    result: list[str] = []
    index, length = 0, len(text)
    while index < length:
        character = text[index]
        following = text[index + 1] if index + 1 < length else ""
        if not _starts(character, following):
            result.append(character)
            index += 1
            continue
        end = index + 1
        if _regional(character) and following and _regional(following):
            end += 1  # a flag is a pair of regional indicators
        while True:
            while end < length and text[end] in _MODIFIERS:
                end += 1
            if (
                end + 1 < length
                and text[end] == ZWJ
                and _starts(text[end + 1], text[end + 2 : end + 3])
            ):
                end += 2
                continue
            break
        result.append(text[index:end])
        index = end
    return result


def is_emoji(token: str) -> bool:
    """Whether a token of tokens() is an emoji sequence."""
    return bool(token) and _starts(token[0], token[1:2])


@lru_cache(maxsize=1)
def font_path() -> Path | None:
    """The colour emoji font of this system, or None.

    ATLAS_EMOJI_FONT names another file; an empty value turns emoji drawing
    off (tests of the path without the font).
    """
    configured = os.environ.get("ATLAS_EMOJI_FONT")
    candidates = (configured,) if configured is not None else FONT_CANDIDATES
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def _format_12(data: bytes) -> set[int]:
    (groups,) = struct.unpack(">I", data[12:16])
    found: set[int] = set()
    for index in range(min(groups, 100_000)):
        start, end, _ = struct.unpack(">III", data[16 + 12 * index : 28 + 12 * index])
        found.update(range(start, min(end, 0x10FFFF) + 1))
    return found


def _format_4(data: bytes) -> set[int]:
    (doubled,) = struct.unpack(">H", data[6:8])
    count = doubled // 2
    ends = struct.unpack(f">{count}H", data[14 : 14 + doubled])
    base = 16 + doubled
    starts = struct.unpack(f">{count}H", data[base : base + doubled])
    deltas = struct.unpack(f">{count}h", data[base + doubled : base + 2 * doubled])
    offsets_at = base + 2 * doubled
    offsets = struct.unpack(f">{count}H", data[offsets_at : offsets_at + doubled])
    found: set[int] = set()
    for segment, (start, end) in enumerate(zip(starts, ends, strict=True)):
        for code in range(start, end + 1):
            if code == 0xFFFF:
                continue
            if offsets[segment] == 0:
                glyph = (code + deltas[segment]) & 0xFFFF
            else:
                at = offsets_at + 2 * segment + offsets[segment] + 2 * (code - start)
                if at + 2 > len(data):
                    continue
                (glyph,) = struct.unpack(">H", data[at : at + 2])
            if glyph:
                found.add(code)
    return found


@lru_cache(maxsize=1)
def covered() -> frozenset[int]:
    """Code points the emoji font has a glyph for (empty without the font)."""
    path = font_path()
    if path is None:
        return frozenset()
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
            (tables,) = struct.unpack(">H", header[4:6])
            directory = handle.read(16 * tables)
            for index in range(tables):
                tag, _, offset, length = struct.unpack(
                    ">4sIII", directory[16 * index : 16 * index + 16]
                )
                if tag != b"cmap":
                    continue
                handle.seek(offset)
                table = handle.read(min(length, 4_000_000))
                (subtables,) = struct.unpack(">H", table[2:4])
                chosen: dict[int, bytes] = {}
                for entry in range(subtables):
                    platform, encoding, at = struct.unpack(
                        ">HHI", table[4 + 8 * entry : 12 + 8 * entry]
                    )
                    (kind,) = struct.unpack(">H", table[at : at + 2])
                    if platform in (0, 3) and kind in (4, 12):
                        chosen.setdefault(kind, table[at:])
                if 12 in chosen:
                    return frozenset(_format_12(chosen[12]))
                if 4 in chosen:
                    return frozenset(_format_4(chosen[4]))
    except (OSError, struct.error, ValueError):
        return frozenset()
    return frozenset()


def drawable(token: str) -> bool:
    """Whether this emoji sequence can be drawn as a picture."""
    face = covered()
    if not face or not is_emoji(token):
        return False
    return all(
        ord(character) in face
        for character in token
        if character not in _MODIFIERS and character != ZWJ
    )


@lru_cache(maxsize=1)
def _face():  # type: ignore[no-untyped-def]
    from PIL import ImageFont, features

    path = font_path()
    if path is None:
        return None
    engine = ImageFont.Layout.RAQM if features.check("raqm") else ImageFont.Layout.BASIC
    return ImageFont.truetype(str(path), STRIKE, layout_engine=engine)


@dataclass(frozen=True)
class Picture:
    """A drawn emoji: PNG bytes, and its advance, ascent and height in ems."""

    png: bytes
    advance: float
    ascent: float
    height: float


@lru_cache(maxsize=512)
def picture(token: str) -> Picture | None:
    """A drawable emoji as a PNG picture with its metrics, or None."""
    if not drawable(token):
        return None
    from PIL import Image, ImageDraw

    face = _face()
    if face is None:
        return None
    ascent, descent = face.getmetrics()
    advance = face.getlength(token)
    width = max(1, int(round(advance)))
    image = Image.new("RGBA", (width, ascent + descent), (0, 0, 0, 0))
    ImageDraw.Draw(image).text((0, 0), token, font=face, embedded_color=True)
    if image.getbbox() is None:
        return None
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return Picture(
        buffer.getvalue(),
        advance / STRIKE,
        ascent / STRIKE,
        (ascent + descent) / STRIKE,
    )


def advance(token: str) -> float:
    """The width of a drawable emoji in ems (Noto: 1.245)."""
    drawn = picture(token)
    return drawn.advance if drawn is not None else 0.0


def without_pictures(text: str) -> str:
    """The text without its drawable emoji: what a PDF page holds as text."""
    return "".join(
        token for token in tokens(text) if not (is_emoji(token) and drawable(token))
    )


# A paragraph of emoji alone is a picture, set at this size in points: a
# picture book gives each word its picture, not an icon at the size of text.
PICTURE_SIZE = 48.0


def pictures_only(text: str) -> bool:
    """Whether a text is only drawable emoji (and spaces)."""
    found = False
    for token in tokens(text):
        if token.isspace():
            continue
        if not (is_emoji(token) and drawable(token)):
            return False
        found = True
    return found


def reset() -> None:
    """Forget the font found (tests set ATLAS_EMOJI_FONT, then call this)."""
    for cached in (font_path, covered, _face, picture):
        cached.cache_clear()
