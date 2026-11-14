"""Words set on a CPU picture by code (a name on a logo, a greeting on a card).

The image model letters badly, so it is asked for a wordless picture with a
plain empty band (rewrite.RESERVE) and code sets the words the user wrote,
as the diagram labels are set:

1. the band: the horizontal band of the picture with the least detail
   (mean gradient), the middle of the picture slightly penalised since the
   subject is usually there;
2. the size: the largest that fits the band and 90 % of the width, lines
   broken at spaces when a line is too wide (at most four lines);
3. the colour: dark or white ink, whichever contrasts most with the
   background under the words; when the darkest or lightest part of that
   background gives less than 4.5:1 (WCAG AA), a plate goes behind them.

The font is Liberation Sans Bold (SIL Open Font License 1.1), which covers
Latin, Greek and Cyrillic, so accents, apostrophes and quotation marks are
drawn as written; a character the font lacks is reported. The same
placement is returned so the SVG artifact holds the words as real SVG text
(cpu_finish.document), which a later change can edit.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from services.orchestrator.pictures import render

FONT_FILE = "LiberationSans-Bold.ttf"
SVG_FONT = "'Liberation Sans', Arial, Helvetica, sans-serif"
MIN_SIZE = 20
MAX_SIZE = 110
MAX_LINES = 4
CONTRAST = 4.5
LINE_HEIGHT = 1.2
WIDTH_SHARE = 0.9
DARK = (28, 28, 34)
LIGHT = (255, 255, 255)
PLATE_OPACITY = 0.86
# Share of the picture height a band takes for one line, then for each more.
BAND_FIRST = 0.15
BAND_MORE = 0.08
BAND_MOST = 0.36
MIDDLE_PENALTY = 0.6
# A band grows over the plain rows next to it (detail below this level, or
# below 1.5 times its own) so short words in a wide empty area get bigger.
PLAIN_ROW = 0.004
NOTDEF = "\U0010fffd"

Font = ImageFont.FreeTypeFont


def font(size: int) -> Font | None:
    for folder in render.font_directories():
        try:
            return ImageFont.truetype(f"{folder}/{FONT_FILE}", size)
        except OSError:
            continue
    return None


def missing_glyphs(face: Font, words: str) -> list[str]:
    """Characters of `words` the font draws as its missing-glyph box."""
    empty = face.getmask(NOTDEF)
    blank = (empty.size, bytes(empty))
    found: list[str] = []
    for char in dict.fromkeys(words):
        if char.isspace():
            continue
        mask = face.getmask(char)
        if (mask.size, bytes(mask)) == blank and char not in found:
            found.append(char)
    return found


def luminance(rgb: np.ndarray) -> np.ndarray:
    """WCAG relative luminance of sRGB values (0..255), last axis RGB."""
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    linear = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return np.asarray(linear @ np.array([0.2126, 0.7152, 0.0722]))


def contrast(first: float, second: float) -> float:
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def detail(picture: Image.Image, share: float = WIDTH_SHARE) -> np.ndarray:
    """Per row of the picture, the mean gradient (0..1) of the centred
    columns the words take (`share` of the width)."""
    grey = np.asarray(picture.convert("L"), dtype=np.float64) / 255.0
    margin = int(grey.shape[1] * (1 - min(max(share, 0.1), 1.0)) / 2)
    grey = grey[:, margin : grey.shape[1] - margin]
    gx = np.abs(np.diff(grey, axis=1))[:-1, :]
    gy = np.abs(np.diff(grey, axis=0))[:, :-1]
    rows = np.asarray((gx + gy).mean(axis=1))
    return np.concatenate([rows, rows[-1:]])


def band_height(height: int, lines: int) -> int:
    return int(height * min(BAND_MOST, BAND_FIRST + BAND_MORE * (lines - 1)))


def band(
    picture: Image.Image, lines: int, share: float = WIDTH_SHARE
) -> tuple[int, int, float]:
    """(top, bottom, mean detail) of the emptiest band for `lines` lines of
    words `share` of the picture wide, grown over the plain rows around it."""
    height = picture.height
    size = band_height(height, lines)
    rows = detail(picture, share)
    cumulative = np.concatenate([[0.0], np.cumsum(rows)])
    edge = int(height * 0.02)
    best: tuple[float, int, float] | None = None
    for top in range(edge, max(edge + 1, height - size - edge), max(1, height // 96)):
        mean = float((cumulative[top + size] - cumulative[top]) / size)
        centre = (top + size / 2) / height
        middle = 1.0 - abs(centre - 0.5) * 2.0
        score = mean * (1.0 + MIDDLE_PENALTY * middle) + 1e-4 * middle
        if best is None or score < best[0]:
            best = (score, top, mean)
    assert best is not None
    top, bottom = best[1], best[1] + size
    plain = max(PLAIN_ROW, 1.5 * best[2])
    most = int(height * BAND_MOST)
    while bottom - top < most and top > edge and rows[top - 1] <= plain:
        top -= 1
    while bottom - top < most and bottom < height - edge and rows[bottom] <= plain:
        bottom += 1
    return top, bottom, best[2]


def _wrap(face: Font, line: str, width: float) -> list[str]:
    words = line.split(" ")
    out: list[str] = []
    current = ""
    for word in words:
        trial = (current + " " + word).strip()
        if current and face.getlength(trial) > width:
            out.append(current)
            current = word
        else:
            current = trial
    if current:
        out.append(current)
    return out


def _balanced(face: Font, line: str, parts: int) -> list[str] | None:
    """`line` broken at spaces into `parts` lines of the most even width
    (ties: the longer line first), or None when it has too few words."""
    words = line.split(" ")
    if len(words) < parts:
        return None
    if len(words) > 14:
        return _wrap(face, line, face.getlength(line) / parts + 1)[:parts]
    best: tuple[float, float, list[str]] | None = None
    for cuts in combinations(range(1, len(words)), parts - 1):
        bounds = (0, *cuts, len(words))
        lines = [" ".join(words[i:j]) for i, j in zip(bounds, bounds[1:])]
        widths = [face.getlength(item) for item in lines]
        key = (max(widths), -widths[0])
        if best is None or key < best[:2]:
            best = (key[0], key[1], lines)
    return best[2] if best else None


def _largest(lines: list[str], width: float, height: float) -> int:
    """The largest size (MIN_SIZE at least) at which `lines` fit the box."""
    for size in range(MAX_SIZE, MIN_SIZE - 1, -1):
        face = font(size)
        if face is None:
            return MIN_SIZE
        widest = max(face.getlength(line) for line in lines)
        if widest <= width and len(lines) * size * LINE_HEIGHT <= height:
            return size
    return MIN_SIZE


def fit(words: list[str], width: float, height: float) -> tuple[int, list[str]]:
    """(size, lines): the largest size at which the words fit the box. One
    line of words is broken into even lines only when that makes them a
    quarter bigger for each line added; several lines (as the user wrote
    them) are broken at spaces only when a line is too wide."""
    face = font(100)
    if face is None:
        return MIN_SIZE, list(words)
    if len(words) == 1:
        best_size, best_lines = _largest(words, width, height), list(words)
        for parts in range(2, MAX_LINES + 1):
            lines = _balanced(face, words[0], parts)
            if lines is None:
                break
            size = _largest(lines, width, height)
            if size >= best_size * 1.25:
                best_size, best_lines = size, lines
        return best_size, best_lines
    lines = list(words)
    for size in range(MAX_SIZE, MIN_SIZE - 1, -2):
        face = font(size)
        if face is None:
            return MIN_SIZE, lines
        lines = [part for line in words for part in _wrap(face, line, width)]
        if len(lines) > MAX_LINES:
            continue
        widest = max(face.getlength(line) for line in lines)
        if widest <= width and len(lines) * size * LINE_HEIGHT <= height:
            return size, lines
    return MIN_SIZE, lines


def _hex(colour: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*colour)


def ink(region: np.ndarray) -> dict[str, Any]:
    """The ink and plate for words over `region` (H x W x 3, 0..255)."""
    pixels = region.reshape(-1, 3).astype(np.float64)
    values = luminance(pixels)
    median = float(np.median(values))
    dark = float(luminance(np.array(DARK)))
    use_dark = contrast(dark, median) >= contrast(1.0, median)
    ink_lum = dark if use_dark else 1.0
    # The worst case is the background part nearest the ink: the darkest
    # part under dark ink, the lightest under white ink.
    order = np.argsort(values)
    worst_rgb = pixels[order[int((len(order) - 1) * (0.05 if use_dark else 0.95))]]
    worst = contrast(ink_lum, float(luminance(worst_rgb)))
    result: dict[str, Any] = {
        "fill": _hex(DARK if use_dark else LIGHT),
        "contrast": round(worst, 2),
        "plate": None,
    }
    if worst < CONTRAST:
        plate = np.array(LIGHT if use_dark else DARK, dtype=np.float64)
        opacity = PLATE_OPACITY
        mixed = plate * opacity + worst_rgb * (1 - opacity)
        if contrast(ink_lum, float(luminance(mixed))) < CONTRAST:
            opacity, mixed = 1.0, plate
        result["plate"] = _hex(LIGHT if use_dark else DARK)
        result["plate_opacity"] = opacity
        result["contrast"] = round(contrast(ink_lum, float(luminance(mixed))), 2)
    return result


def set_words(
    picture: Image.Image, words: list[str]
) -> tuple[Image.Image, dict[str, Any]]:
    """The picture with the words set, and their placement (for the SVG)."""
    picture = picture.convert("RGB")
    width, height = picture.size
    words = [" ".join(line.split()) for line in words if line.strip()]
    if not words:
        return picture, {}
    # As many band lines as the words need at a size that fits such a band.
    count = len(words)
    while True:
        trial = fit(words, width * WIDTH_SHARE, band_height(height, count) * 0.92)
        if len(trial[1]) <= count or count >= MAX_LINES:
            break
        count += 1
    face = font(trial[0])
    share = WIDTH_SHARE
    if face is not None:
        share = max(face.getlength(line) for line in trial[1]) / width + 0.06
    top, bottom, busy = band(picture, count, share)
    size, lines = fit(words, width * WIDTH_SHARE, (bottom - top) * 0.92)
    face = font(size)
    if face is None:
        return picture, {"error": "font_missing"}
    step = size * LINE_HEIGHT
    ascent, descent = face.getmetrics()
    block = step * len(lines)
    start = (top + bottom) / 2 - block / 2
    baselines = [
        start + k * step + (step - (ascent + descent)) / 2 + ascent
        for k in range(len(lines))
    ]
    widest = max(face.getlength(line) for line in lines)
    pad = size * 0.35
    box = [
        max(0.0, width / 2 - widest / 2 - pad),
        max(0.0, start - pad * 0.4),
        min(float(width), width / 2 + widest / 2 + pad),
        min(float(height), start + block + pad * 0.4),
    ]
    region = np.asarray(picture)[
        int(box[1]) : max(int(box[3]), int(box[1]) + 1),
        int(box[0]) : max(int(box[2]), int(box[0]) + 1),
    ]
    choice = ink(region)
    canvas = picture.convert("RGBA")
    if choice["plate"]:
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        plate = tuple(int(choice["plate"][i : i + 2], 16) for i in (1, 3, 5))
        alpha = int(round(255 * float(choice["plate_opacity"])))
        ImageDraw.Draw(overlay).rounded_rectangle(
            box, radius=size * 0.3, fill=(*plate, alpha)
        )
        canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)
    fill = tuple(int(choice["fill"][i : i + 2], 16) for i in (1, 3, 5))
    for line, baseline in zip(lines, baselines):
        draw.text((width / 2, baseline), line, font=face, fill=fill, anchor="ms")
    placement = {
        "lines": lines,
        "size": size,
        "x": round(width / 2, 1),
        "baselines": [round(b, 1) for b in baselines],
        "box": [round(v, 1) for v in box],
        "band": [top, bottom],
        "band_detail": round(busy, 4),
        "missing": missing_glyphs(face, "".join(lines)),
        **choice,
    }
    return canvas.convert("RGB"), placement


def svg_elements(placement: dict[str, Any]) -> str:
    """The plate and the words of a placement as SVG elements (real text)."""
    from xml.sax.saxutils import escape, quoteattr

    if not placement.get("lines"):
        return ""
    parts: list[str] = []
    if placement.get("plate"):
        x0, y0, x1, y1 = placement["box"]
        parts.append(
            f'<rect x="{x0}" y="{y0}" width="{round(x1 - x0, 1)}" '
            f'height="{round(y1 - y0, 1)}" rx="{round(placement["size"] * 0.3, 1)}" '
            f'fill="{placement["plate"]}" '
            f'fill-opacity="{placement.get("plate_opacity", 1)}"/>'
        )
    for line, baseline in zip(placement["lines"], placement["baselines"]):
        parts.append(
            f'<text x="{placement["x"]}" y="{baseline}" text-anchor="middle" '
            f'font-family={quoteattr(SVG_FONT)} font-weight="bold" '
            f'font-size="{placement["size"]}" fill="{placement["fill"]}">'
            f"{escape(line)}</text>"
        )
    return '<g class="words">' + "".join(parts) + "</g>"
