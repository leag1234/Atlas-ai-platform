"""Where the parts of a wordless diagram are drawn: one vision call per picture.

The words come from the rewrite (the user's language), never from the
picture; the vision model only says where each named part is, as a point in
0..1000 picture coordinates. The gateway runs the call on its "locate" role
(spike round 25: the model declared there placed every part; the vision
models tried first answered guessed centres or swapped axes). Code then
places the labels (cpu_finish.place_labels).
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_TOKENS = 600
SYSTEM = (
    "You locate parts in a picture. For each label in the list, look where that "
    "part is actually drawn and give the point (x, y) at the centre of that drawn "
    "part, in relative coordinates from 0 to 1000 (x from the left edge, y from "
    "the top edge). Different parts are at different places: check each one. If "
    "the part is not drawn, give null. "
    'Reply with JSON only: {"points": {"<label>": [x, y] or null, ...}}'
)
PAIR = r"\[\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\]"


def messages(labels: list[str], image_url: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Labels: " + json.dumps(labels, ensure_ascii=False),
                },
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        },
    ]


def _point(value: Any) -> tuple[float, float] | None:
    """[x, y], or the first of several ([[x, y], ...] or point_2d objects)."""
    if isinstance(value, dict):
        value = value.get("point_2d", value.get("point"))
    if (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    ):
        x, y = float(value[0]), float(value[1])
        if 0 <= x <= 1000 and 0 <= y <= 1000:
            return x, y
        return None
    if isinstance(value, list) and value:
        return _point(value[0])
    return None


def parse_points(text: str, labels: list[str]) -> dict[str, tuple[float, float]]:
    """The point of each label the reply places (0..1000); others are absent."""
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.strip("`")
        clean = clean.split("\n", 1)[1] if "\n" in clean else clean
    found: dict[str, tuple[float, float]] = {}
    start, end = clean.find("{"), clean.rfind("}")
    raw: Any = None
    if 0 <= start < end:
        try:
            raw = json.loads(clean[start : end + 1])
        except ValueError:
            raw = None
    points = raw.get("points", raw) if isinstance(raw, dict) else None
    if isinstance(points, dict):
        for label in labels:
            point = _point(points.get(label))
            if point is not None:
                found[label] = point
        return found
    # Broken JSON: read "label": [x, y] pairs wherever they are.
    for label in labels:
        match = re.search(
            re.escape(json.dumps(label, ensure_ascii=False)) + r"\s*:\s*" + PAIR, clean
        )
        if match:
            point = _point([float(match.group(1)), float(match.group(2))])
            if point is not None:
                found[label] = point
    return found
