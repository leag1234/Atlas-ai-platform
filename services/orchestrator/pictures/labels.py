"""Labels of a diagram placed by code (round 16 rule on the painter's SVG).

The painter writes no label text: it puts each part to be labelled in its own
group with data-label="the label". Code then writes every label in the side
margin nearest its part, stacked by height, with a leader line to a point well
inside the part (the pixel of the group's mask nearest its centre of mass, at
least 2 px from its edge). Leader lines that cross are uncrossed by swapping
the two labels' slots until none cross (each swap shortens the total length,
so it ends). The block is one group appended last, above the drawing, so its
text elements are the labels the judge looks for.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

import numpy as np

from services.orchestrator.pictures import ndimage as ndi
from services.orchestrator.pictures.geometry import Geometry, norm_id, tag

NS = "http://www.w3.org/2000/svg"
Q = "{%s}" % NS
ET.register_namespace("", NS)
MAX_LABELS = 16
MAX_CHARS = 40
# Normalised frame: 1024 units across, margins as in round 16.
SPAN = 1024.0
LEFT_MARGIN, RIGHT_MARGIN = 200.0, 824.0
MIN_FONT, MAX_FONT = 10, 28

Point = tuple[float, float]


def declared_labels(svg: str) -> list[dict[str, str]]:
    """{"id", "text"} of every group with an id and a data-label, in document
    order (text trimmed to 40 characters, at most 16 labels)."""
    out: list[dict[str, str]] = []
    for node in ET.fromstring(svg).iter():
        text = re.sub(r"\s+", " ", node.get("data-label") or "").strip()
        ident = node.get("id")
        if tag(node) != "g" or not text or not ident:
            continue
        out.append({"id": ident, "text": text[:MAX_CHARS].rstrip()})
        if len(out) == MAX_LABELS:
            break
    return out


def crosses(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    """Whether segments p1-p2 and q1-q2 cross (touching ends do not count)."""

    def o(a: Point, b: Point, c: Point) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    return (o(p1, p2, q1) * o(p1, p2, q2) < 0) and (o(q1, q2, p1) * o(q1, q2, p2) < 0)


def _anchor(g: Geometry, name: str) -> tuple[Point, str] | None:
    """Normalised point inside the group (see the module rule), or its box
    centre when it is too thin, or None when nothing of it is drawn."""
    m = g.mask(norm_id(name))
    if not m.any():
        return None
    scale = SPAN / g.fw

    def point(row: float, column: float) -> Point:
        return ((column + 0.5) * scale, (row + 0.5) * scale)

    inner = ndi.distance_transform_edt(m)
    if inner.max() < 2.0:
        box = g.bbox(m)
        assert box is not None
        return point((box[1] + box[3]) / 2, (box[0] + box[2]) / 2), "box centre"
    cy, cx = ndi.center_of_mass(m)
    ys, xs = np.nonzero(inner >= 2.0)
    j = int(np.argmin((xs - cx) ** 2 + (ys - cy) ** 2))
    return point(float(ys[j]), float(xs[j])), "group"


def place(
    svg: str, labels: list[dict[str, str]], group_id: str = "labels"
) -> tuple[str, dict[str, Any]]:
    """The SVG with the labels written by code, and a report (labels placed,
    swaps, crossings left, labels whose part is not drawn)."""
    g = Geometry(svg, frame=512, pad=0.0)
    x0, y0, width, height = g.vb
    s = width / SPAN  # viewBox units per normalised unit
    bottom = height / s
    report: dict[str, Any] = {
        "labels": [],
        "swaps": 0,
        "crossings_left": 0,
        "missing": [],
    }
    items: list[dict[str, Any]] = []
    for label in labels[:MAX_LABELS]:
        found = _anchor(g, label["id"])
        if found is None:
            report["missing"].append(label["id"])
            continue
        items.append(
            {"text": label["text"][:MAX_CHARS], "anchor": found[0], "how": found[1]}
        )
    block = ET.Element(Q + "g", {"id": group_id})
    for side in ("left", "right"):
        group = sorted(
            [it for it in items if (it["anchor"][0] < SPAN / 2) == (side == "left")],
            key=lambda it: it["anchor"][1],
        )
        if not group:
            continue
        room = (LEFT_MARGIN if side == "left" else SPAN - RIGHT_MARGIN) - 20
        longest = max(max(len(it["text"]), 1) for it in group)
        fs = int(max(MIN_FONT, min(MAX_FONT, room / (0.6 * longest))))
        step = fs * 1.9
        ys: list[float] = []
        for it in group:
            ys.append(max(it["anchor"][1], (ys[-1] + step) if ys else fs + 6))
        if ys[-1] > bottom - fs - 6:
            shift = ys[-1] - (bottom - fs - 6)
            ys = [max(fs + 6, y - shift) for y in ys]
        lx = (LEFT_MARGIN - 8) if side == "left" else (RIGHT_MARGIN + 8)
        for _ in range(200):
            swapped = False
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    if crosses(
                        (lx, ys[i]), group[i]["anchor"], (lx, ys[j]), group[j]["anchor"]
                    ):
                        group[i], group[j] = group[j], group[i]
                        report["swaps"] += 1
                        swapped = True
            if not swapped:
                break
        n = len(group)
        report["crossings_left"] += sum(
            crosses((lx, ys[i]), group[i]["anchor"], (lx, ys[j]), group[j]["anchor"])
            for i in range(n)
            for j in range(i + 1, n)
        )
        for it, y in zip(group, ys):
            ax, ay = it["anchor"]
            tw = 0.58 * fs * len(it["text"])
            if side == "left":
                tx = max(lx - 6, 10 + tw)  # a long label never leaves the frame
                rx = tx - tw - 6
            else:
                tx = min(lx + 6, SPAN - 10 - tw)
                rx = tx - 6

            def u(v: float) -> str:
                return f"{v * s:.1f}"

            def ux(v: float) -> str:
                return f"{x0 + v * s:.1f}"

            def uy(v: float) -> str:
                return f"{y0 + v * s:.1f}"

            ET.SubElement(
                block,
                Q + "line",
                {
                    "x1": ux(lx),
                    "y1": uy(y),
                    "x2": ux(ax),
                    "y2": uy(ay),
                    "stroke": "#1e1e1e",
                    "stroke-width": u(2.2),
                },
            )
            ET.SubElement(
                block,
                Q + "circle",
                {
                    "cx": ux(ax),
                    "cy": uy(ay),
                    "r": u(4.5),
                    "fill": "#1e1e1e",
                    "stroke": "#ffffff",
                    "stroke-width": u(1.5),
                },
            )
            ET.SubElement(
                block,
                Q + "rect",
                {
                    "x": ux(rx),
                    "y": uy(y - fs * 0.75),
                    "width": u(tw + 12),
                    "height": u(fs * 1.5),
                    "rx": u(5),
                    "fill": "#ffffff",
                    "fill-opacity": "0.88",
                },
            )
            text = ET.SubElement(
                block,
                Q + "text",
                {
                    "x": ux(tx),
                    "y": uy(y),
                    "font-family": "Liberation Sans",
                    "font-weight": "bold",
                    "font-size": u(fs),
                    "fill": "#141414",
                    "dominant-baseline": "middle",
                    "text-anchor": "end" if side == "left" else "start",
                },
            )
            text.text = it["text"]  # ElementTree escapes it on output
            report["labels"].append(
                {
                    "text": it["text"],
                    "side": side,
                    "y": round(y),
                    "anchor": [round(ax), round(ay)],
                    "how": it["how"],
                }
            )
    root = ET.fromstring(svg)
    for node in root.iter():
        if node.get("id") == group_id:
            # Keep ids unique: the painter's own group of that name steps aside.
            node.set("id", f"{group_id}-painter")
            report["renamed"] = True
    if len(block):
        root.append(block)
    return ET.tostring(root, encoding="unicode"), report
