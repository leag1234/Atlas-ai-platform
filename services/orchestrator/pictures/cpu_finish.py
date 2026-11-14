"""A picture from the CPU server, made ready to show (run in the picture worker).

Decodes the server's PNG (bounded size), places the labels of a diagram
when points are given, and returns the image to show, a square thumbnail for
the contact sheet and the SVG document that holds the picture as the
conversation's drawing artifact (the picture embedded as a JPEG).

Labels (rule of the measured label16 layout, ported to raster in spike round
25): the picture gets white side margins; each word goes into the margin of
its half, stacked by height with a minimum step, with a leader line to its
part; slots are swapped until no two leader lines cross.
"""

from __future__ import annotations

import base64
import binascii
import io
from typing import Any
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw, ImageFont

from services.orchestrator.pictures import render

PICTURE_PX = 768
MARGIN_PX = 300
THUMB_PX = 512
MAX_INPUT_BYTES = 6_000_000
MAX_PNG_BYTES = 1_900_000
INK = (30, 30, 30)
TEXT = (20, 20, 20)
WHITE = (255, 255, 255)
Point = tuple[float, float]


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for folder in render.font_directories():
        try:
            return ImageFont.truetype(f"{folder}/LiberationSans-Bold.ttf", size)
        except OSError:
            continue
    return ImageFont.load_default()


def _width(font: ImageFont.FreeTypeFont | ImageFont.ImageFont, text: str) -> float:
    return float(font.getlength(text))


def crossing(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    """Strict crossing of segments p1p2 and q1q2 (label16.cross)."""

    def side(a: Point, b: Point, c: Point) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    return (side(p1, p2, q1) * side(p1, p2, q2) < 0) and (
        side(q1, q2, p1) * side(q1, q2, p2) < 0
    )


def layout(
    anchors: list[tuple[str, Point]], height: float, margin: float, width: float
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Slots of the labels: [{"text", "side", "x", "y", "anchor", "size"}].

    anchors are in canvas pixels; the left half's words go into the left
    margin, the others into the right one.
    """
    placed: list[dict[str, Any]] = []
    report = {"swaps": 0, "crossings_left": 0}
    for side in ("left", "right"):
        group = sorted(
            [a for a in anchors if (a[1][0] < width / 2) == (side == "left")],
            key=lambda a: a[1][1],
        )
        if not group:
            continue
        size = 26
        while size > 14 and max(_width(_font(size), a[0]) for a in group) > margin - 34:
            size -= 1
        step = size * 1.9
        slots: list[float] = []
        for _, (_, y) in group:
            slots.append(max(y, (slots[-1] + step) if slots else size + 6))
        if slots[-1] > height - size - 6:
            shift = slots[-1] - (height - size - 6)
            slots = [max(size + 6, y - shift) for y in slots]
        x = (margin - 8) if side == "left" else (width - margin + 8)
        for _ in range(200):
            swapped = False
            for i in range(len(group)):
                for k in range(i + 1, len(group)):
                    if crossing((x, slots[i]), group[i][1], (x, slots[k]), group[k][1]):
                        group[i], group[k] = group[k], group[i]
                        report["swaps"] += 1
                        swapped = True
            if not swapped:
                break
        n = len(group)
        report["crossings_left"] += sum(
            crossing((x, slots[i]), group[i][1], (x, slots[k]), group[k][1])
            for i in range(n)
            for k in range(i + 1, n)
        )
        for (text, anchor), y in zip(group, slots):
            placed.append(
                {
                    "text": text,
                    "side": side,
                    "x": x,
                    "y": y,
                    "anchor": anchor,
                    "size": size,
                }
            )
    return placed, report


def place_labels(
    picture: Image.Image, points: dict[str, Point], labels: list[str]
) -> tuple[Image.Image, dict[str, Any]]:
    """The picture with side margins and its labels; points in 0..1000."""
    size = picture.width
    width = size + 2 * MARGIN_PX
    canvas = Image.new("RGB", (width, picture.height), WHITE)
    canvas.paste(picture, (MARGIN_PX, 0))
    anchors: list[tuple[str, Point]] = []
    missing: list[str] = []
    for label in labels:
        point = points.get(label)
        if point is None:
            missing.append(label)
            continue
        anchors.append(
            (
                label,
                (
                    MARGIN_PX + point[0] / 1000 * size,
                    point[1] / 1000 * picture.height,
                ),
            )
        )
    placed, report = layout(anchors, picture.height, MARGIN_PX, width)
    draw = ImageDraw.Draw(canvas)
    for item in placed:
        font = _font(int(item["size"]))
        x, y = float(item["x"]), float(item["y"])
        ax, ay = item["anchor"]
        draw.line([(x, y), (ax, ay)], fill=INK, width=3)
        draw.ellipse([ax - 5, ay - 5, ax + 5, ay + 5], fill=INK, outline=WHITE, width=2)
        text_width = _width(font, item["text"])
        left = x - 8 - text_width if item["side"] == "left" else x + 8
        half = item["size"] * 0.75
        draw.rounded_rectangle(
            [left - 5, y - half, left + text_width + 5, y + half], radius=5, fill=WHITE
        )
        draw.text((left, y), item["text"], font=font, fill=TEXT, anchor="lm")
    return canvas, {
        "labels": len(placed),
        "missing": missing,
        "swaps": report["swaps"],
        "crossings_left": report["crossings_left"],
    }


def _encode(picture: Image.Image) -> dict[str, str]:
    buffer = io.BytesIO()
    picture.save(buffer, "PNG", optimize=True)
    if buffer.tell() <= MAX_PNG_BYTES:
        return {
            "mime": "image/png",
            "data": base64.b64encode(buffer.getvalue()).decode(),
        }
    buffer = io.BytesIO()
    picture.convert("RGB").save(buffer, "JPEG", quality=90)
    return {"mime": "image/jpeg", "data": base64.b64encode(buffer.getvalue()).decode()}


def _square(picture: Image.Image) -> Image.Image:
    side = max(picture.size)
    square = Image.new("RGB", (side, side), WHITE)
    square.paste(picture, ((side - picture.width) // 2, (side - picture.height) // 2))
    return square.resize((THUMB_PX, THUMB_PX), Image.Resampling.LANCZOS)


def document(picture: Image.Image, title: str) -> str:
    """The SVG document of the artifact: the picture embedded as a JPEG."""
    buffer = io.BytesIO()
    picture.convert("RGB").save(buffer, "JPEG", quality=88)
    data = base64.b64encode(buffer.getvalue()).decode("ascii")
    width, height = picture.size
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}">'
        f"<title>{escape(title)}</title>"
        f'<image href="data:image/jpeg;base64,{data}" x="0" y="0" '
        f'width="{width}" height="{height}"/></svg>'
    )


def finish_picture(payload: dict[str, Any]) -> dict[str, Any]:
    """{"image" (base64 PNG), "title", "labels", "points"} -> image, thumb, svg."""
    raw = str(payload.get("image") or "")
    if len(raw) > MAX_INPUT_BYTES * 4 // 3 + 4:
        raise ValueError("picture_too_large")
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("invalid_picture") from None
    Image.MAX_IMAGE_PIXELS = 4 * PICTURE_PX * PICTURE_PX
    opened = Image.open(io.BytesIO(data))
    if opened.format != "PNG" or max(opened.size) > 2 * PICTURE_PX:
        raise ValueError("invalid_picture")
    picture: Image.Image = opened.convert("RGB")
    if picture.size != (PICTURE_PX, PICTURE_PX):
        picture = picture.resize((PICTURE_PX, PICTURE_PX), Image.Resampling.LANCZOS)
    report: dict[str, Any] = {}
    labels = [str(label) for label in payload.get("labels") or []]
    raw_points = payload.get("points") or {}
    if labels:
        points = {
            str(k): (float(v[0]), float(v[1]))
            for k, v in raw_points.items()
            if isinstance(v, (list, tuple)) and len(v) == 2
        }
        picture, report = place_labels(picture, points, labels)
    title = str(payload.get("title") or "")
    return {
        "image": _encode(picture),
        "thumb": _encode(_square(picture)),
        "svg": document(picture, title),
        "report": report,
    }
