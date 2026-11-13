"""The code steps of one draft, and the contact sheet, run in the picture worker.

Order of one draft (spike round 23, plus labels by code for diagrams):
sanitise; animal kit (when on); body kit; labels placed by code when the
painter declared them; judge and the four code checks (for ordering only);
face kit; contour finish; renders. A step that fails keeps its input and
says so in the report: a kit never costs the user a draft.
"""

from __future__ import annotations

import base64
import io
import math
import time
from collections.abc import Callable
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from services.orchestrator.pictures import render
from services.orchestrator.pictures.sanitise import sanitise

PICTURE_PX = 1024
THUMB_PX = 512
# image_store accepts at most 2 MiB per image: a heavier PNG becomes a JPEG.
MAX_PNG_BYTES = 1_900_000
SHEET_GUTTER = 12
SHEET_ACCENT = (37, 99, 235)


def _encode(picture: Image.Image) -> tuple[str, str]:
    """(mime, base64) of a render: PNG, or JPEG when the PNG is too heavy."""
    buffer = io.BytesIO()
    picture.save(buffer, "PNG")
    if buffer.tell() <= MAX_PNG_BYTES:
        return "image/png", base64.b64encode(buffer.getvalue()).decode("ascii")
    buffer = io.BytesIO()
    picture.convert("RGB").save(buffer, "JPEG", quality=90)
    return "image/jpeg", base64.b64encode(buffer.getvalue()).decode("ascii")


def _step(
    report: dict[str, Any],
    name: str,
    svg: str,
    function: Callable[[str], tuple[str, Any]],
) -> str:
    """Run one kit step; on failure keep its input and record the error class."""
    started = time.monotonic()
    try:
        result, detail = function(svg)
    except Exception as error:  # noqa: BLE001 - a kit never drops the draft
        report[name] = {"error": type(error).__name__}
        result = svg
    else:
        report[name] = _summary(name, detail)
    report[name]["seconds"] = round(time.monotonic() - started, 2)
    return result


def _summary(name: str, detail: Any) -> dict[str, Any]:
    """The few numbers of a kit report worth keeping (no geometry dumps)."""
    if not isinstance(detail, (dict, list)):
        return {}
    if name == "face" and isinstance(detail, list):
        decisions = [str(head.get("decision", "")) for head in detail]
        return {"heads": len(decisions), "kept": decisions.count("kept")}
    if isinstance(detail, dict):
        if name == "finish":
            return {key: len(value) for key, value in detail.items()}
        out: dict[str, Any] = {}
        for key in ("persons", "animals", "invalid", "labels", "missing"):
            value = detail.get(key)
            if isinstance(value, list):
                out[key] = len(value)
        for key in ("swaps", "crossings_left"):
            if isinstance(detail.get(key), int):
                out[key] = detail[key]
        if detail.get("error"):
            out["error"] = "kit_error"
        return out
    return {}


def process_draft(payload: dict[str, Any]) -> dict[str, Any]:
    """One painter draft through the code steps.

    Input: svg (recovered text), checklist, living (id -> bool), animal_kit,
    labels_group. Output: the final SVG, its 1024 px render, a 512 px render
    for the contact sheet, the ordering measure and a short report.
    """
    from services.orchestrator.pictures import (
        animal_kit,
        body_kit,
        face_kit,
        finish,
        labels,
        ordering,
    )

    started = time.monotonic()
    ck: dict[str, Any] = payload["checklist"]
    living: dict[str, bool] = payload.get("living") or {}
    report: dict[str, Any] = {}
    svg = sanitise(str(payload["svg"]))
    if payload.get("animal_kit"):
        svg = _step(report, "animals", svg, lambda s: animal_kit.compose(s, ck, living))
    svg = _step(report, "bodies", svg, lambda s: body_kit.compose(s, ck, living))
    declared = labels.declared_labels(svg)
    if declared:
        group = str(payload.get("labels_group") or "labels")
        svg = _step(
            report, "labels", svg, lambda s: labels.place(s, declared, group_id=group)
        )
    measured = time.monotonic()
    measure = ordering.measure(svg, ck, living)
    report["measure_seconds"] = round(time.monotonic() - measured, 2)
    svg = _step(report, "face", svg, lambda s: face_kit.apply(s, ck))
    svg = _step(
        report,
        "finish",
        svg,
        lambda s: finish.apply(s, finish.measures(s, ck), "auto", True),
    )
    final = sanitise(svg)
    picture = render.image(final, PICTURE_PX, background="#ffffff", mode="RGB")
    mime, data = _encode(picture)
    thumb_mime, thumb = _encode(
        picture.resize((THUMB_PX, THUMB_PX), Image.Resampling.LANCZOS)
    )
    report["seconds"] = round(time.monotonic() - started, 2)
    return {
        "svg": final,
        "image": {"mime": mime, "data": data},
        "thumb": {"mime": thumb_mime, "data": thumb},
        "measure": {
            key: measure.get(key)
            for key in ("J", "Q", "P", "flags", "measure_error")
            if key in measure
        },
        "report": report,
    }


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for folder in render.font_directories():
        try:
            return ImageFont.truetype(f"{folder}/LiberationSans-Bold.ttf", size)
        except OSError:
            continue
    return ImageFont.load_default()


def contact_sheet(payload: dict[str, Any]) -> dict[str, Any]:
    """The candidates side by side, numbered, the suggested one framed.

    Input: thumbs (base64 images in display order). Two columns (one for a
    single candidate); the sheet is a JPEG of at most 1024 px wide.
    """
    thumbs = [
        Image.open(io.BytesIO(base64.b64decode(item))).convert("RGB")
        for item in payload["thumbs"]
    ]
    if not 1 <= len(thumbs) <= 4:
        raise ValueError("invalid_sheet")
    columns = 1 if len(thumbs) == 1 else 2
    rows = math.ceil(len(thumbs) / columns)
    tile = (PICTURE_PX - SHEET_GUTTER * (columns + 1)) // columns
    width = columns * tile + SHEET_GUTTER * (columns + 1)
    height = rows * tile + SHEET_GUTTER * (rows + 1)
    sheet = Image.new("RGB", (width, height), (245, 245, 243))
    draw = ImageDraw.Draw(sheet)
    font = _font(max(18, tile // 12))
    for index, thumb in enumerate(thumbs):
        x = SHEET_GUTTER + (index % columns) * (tile + SHEET_GUTTER)
        y = SHEET_GUTTER + (index // columns) * (tile + SHEET_GUTTER)
        sheet.paste(thumb.resize((tile, tile), Image.Resampling.LANCZOS), (x, y))
        if index == 0 and len(thumbs) > 1:
            draw.rectangle(
                (x - 4, y - 4, x + tile + 3, y + tile + 3),
                outline=SHEET_ACCENT,
                width=5,
            )
        radius = max(16, tile // 14)
        cx, cy = x + radius + 10, y + radius + 10
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=SHEET_ACCENT if index == 0 else (30, 30, 30),
            outline=(255, 255, 255),
            width=3,
        )
        draw.text(
            (cx, cy), str(index + 1), fill=(255, 255, 255), font=font, anchor="mm"
        )
    buffer = io.BytesIO()
    sheet.save(buffer, "JPEG", quality=88)
    return {
        "image": {
            "mime": "image/jpeg",
            "data": base64.b64encode(buffer.getvalue()).decode("ascii"),
        }
    }
