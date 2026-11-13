"""The face kit: code redraws the faces the painter declared (round 17a, version 2).

For every living character the painter put its head in <g id="NAME-head"> with
data-facing, data-expression, data-gaze, data-species and data-age, and its
face features in <g id="NAME-face">. The kit

- hides the painter's own features (the face plate it drew is kept);
- finds the face plate (the head shape whose skin shows most) from renders of
  the head group alone;
- draws eyes, brows, nose or muzzle, mouth and cheeks from a small parametric
  set, with a line weight that follows the head size and colours derived from
  the plate; symmetric for front views, shifted for three-quarter views, one
  eye for profiles; bead eyes for people; where the painter's own eyes and
  mouth were (read from their geometry), the kit keeps that layout;
- raises a hat (NAME-hat, or an object the checklist says the character wears
  or has on) that covers the eye band, unless it says data-covers="eyes";
- aligns the eyes on NAME-eyewear, and draws none under eyewear that covers
  the eyes.

A head whose group, face group or key attributes (facing, species) are missing
or invalid keeps the painter's face; the report says why.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from services.orchestrator.pictures import ndimage as ndi
from services.orchestrator.pictures.svg_doc import (
    Q,
    Doc,
    find_named,
    heads_of,
    lum,
    mix,
    mstr,
    rgb_hex,
)

FACINGS = (
    "front",
    "three-quarter-left",
    "three-quarter-right",
    "profile-left",
    "profile-right",
    "back",
)
EXPRESSIONS = (
    "neutral",
    "smile",
    "laugh",
    "surprise",
    "sad",
    "angry",
    "asleep",
    "focused",
)
GAZES = ("forward", "left", "right", "up", "down")
SPECIES = (
    "human",
    "feline",
    "canine",
    "bovine",
    "ovine",
    "porcine",
    "ursine",
    "lagomorph",
    "bird",
    "other",
)
AGES = ("child", "adult", "old")

Box = list[float]


@dataclass
class Plate:
    """The face plate in root user units; row(y) gives its left and right edge."""

    top: float
    bottom: float
    left: float
    right: float
    row: Callable[[float], tuple[float, float]]
    anchors: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- colours
def palette(skin: Sequence[float]) -> dict[str, str]:
    dark = mix(skin, (30, 18, 14), 0.78)
    if lum(skin) < 70:  # a dark face: features in a light ink
        line = mix(skin, (245, 235, 225), 0.75)
        eye: Sequence[float] = (20, 14, 12)
    else:
        line = mix(skin, (40, 22, 16), 0.72)
        eye = dark
    return {
        "skin": rgb_hex(skin),
        "line": rgb_hex(line),
        "eye": rgb_hex(eye),
        "white": "#fbf8f2",
        "cheek": rgb_hex(mix(skin, (235, 105, 110), 0.45)),
        "mouth": rgb_hex(mix(skin, (95, 25, 30), 0.75)),
        "tongue": rgb_hex(mix(skin, (235, 110, 120), 0.7)),
        "muzzle": rgb_hex(mix(skin, (255, 248, 238), 0.45)),
        "nose": rgb_hex(mix(skin, (35, 22, 20), 0.85)),
        "pink": rgb_hex(mix(skin, (230, 130, 140), 0.55)),
        "beak": rgb_hex((236, 160, 52)),
        "iris_cat": rgb_hex((196, 168, 60)),
        "old": rgb_hex(mix(line, skin, 0.35)),
        "brow_old": rgb_hex(mix(line, (235, 235, 230), 0.55)),
    }


# ---------------------------------------------------------------- SVG elements
def element(name: str, **attrs: Any) -> ET.Element:
    """An SVG element; floats are written with two decimals, "_" becomes "-"."""
    return ET.Element(
        Q + name,
        {
            k.replace("_", "-"): (f"{v:.2f}" if isinstance(v, float) else str(v))
            for k, v in attrs.items()
        },
    )


def path(d: str, **attrs: Any) -> ET.Element:
    return element("path", d=d, **attrs)


# ---------------------------------------------------------------- the kit itself
def draw_face(
    g: ET.Element,
    plate: Plate,
    attrs: dict[str, str],
    pal: dict[str, str],
    eyes_mode: str = "draw",
    eyewear_box: Box | None = None,
    lower_eyes: float = 0.0,
) -> tuple[list[Box], list[str]]:
    """Draw the features into g (root user units). Returns the eye boxes (for
    the hat rule) and the names of the parts drawn."""
    facing, expr, gaze = attrs["facing"], attrs["expression"], attrs["gaze"]
    sp, age = attrs["species"], attrs["age"]
    top, bot = plate.top, plate.bottom
    H = bot - top
    W = plate.right - plate.left
    human = sp == "human"
    animal = not human
    # Vertical layout (shares of the plate height). When the painter's own
    # features give an eye line (and a mouth), the frame is remapped so the
    # kit's eye line and mouth land where the painter put them.
    s_e = {"human": 0.47, "bird": 0.42}.get(sp, 0.44) + (0.05 if age == "child" else 0)
    s_m = {"human": 0.79, "bird": 0.62}.get(sp, 0.78)
    anc = plate.anchors
    if anc.get("eye_y") is not None:
        ya = anc["eye_y"]
        if anc.get("mouth_y") is not None and anc["mouth_y"] > ya:
            H2 = (anc["mouth_y"] - ya) / (s_m - s_e)
            if 0.5 * H <= H2 <= 1.6 * H:
                H = H2
        top = ya - s_e * H
    ey_share = s_e + lower_eyes
    ey = top + ey_share * H
    xl, xr = plate.row(ey)
    half = (xr - xl) / 2
    cx = (xl + xr) / 2
    lw = max(1.2, min(6.0, 0.032 * W))
    r = (
        (0.085 if human else 0.10)
        * W
        * (1.15 if age == "child" else 1.0)
        * (1.1 if sp in ("feline", "lagomorph") else 1.0)
    )
    side = 0
    if facing.startswith("three-quarter"):
        side = -1 if facing.endswith("left") else 1
    if facing.startswith("profile"):
        side = -2 if facing.endswith("left") else 2
    gx: float = {"left": -1, "right": 1}.get(gaze, 0)
    gy = {"up": -1, "down": 1}.get(gaze, 0)
    if gaze == "forward" and abs(side) == 1:
        gx = side * 0.6
    if abs(side) == 2:
        gx = side / 2
    if anc.get("single_eye") and abs(side) < 2:
        sgn = (1 if side > 0 else -1) if side else (-1 if anc["eye_x"][0] < cx else 1)
        side = 2 * sgn
        gx = side / 2
        attrs = {**attrs, "facing": "profile-left" if sgn < 0 else "profile-right"}
    parts: list[str] = []
    eyes: list[tuple[Any, Any, float, float]]  # (x, y, scale_x, scale)
    if side == 0:
        axis = cx
        dx = (0.40 if animal and sp not in ("bird",) else 0.36) * half
        eyes = [(axis - dx, ey, 1.0, 1.0), (axis + dx, ey, 1.0, 1.0)]
    elif abs(side) == 1:
        axis = cx + side * 0.28 * half
        # The eye on the side away from the turn shows whole.
        near = (axis - side * 0.40 * half, ey, 1.0, 1.0)
        far = (axis + side * 0.30 * half, ey, 0.72, 0.95)
        eyes = [near, far]
    else:
        edge = xl if side < 0 else xr
        axis = edge
        eyes = [(edge - (side // 2) * 0.36 * W, ey, 0.8, 1.0)]
    if anc.get("eye_x") and abs(side) < 2 and len(anc["eye_x"]) == 2:
        xa, xb = sorted(anc["eye_x"])
        mid, d = (xa + xb) / 2, (xb - xa) / 2
        d = max(0.22 * half, min(0.55 * half, d))
        r = max(0.35 * d, min(r, 0.62 * d))
        if side == 0:
            axis = mid
            eyes = [(mid - d, ey, 1.0, 1.0), (mid + d, ey, 1.0, 1.0)]
        else:
            axis = mid + side * 0.25 * d
            eyes = [
                (mid - side * d, ey, 1.0, 1.0),
                (mid + side * d * 0.9, ey, 0.72, 0.95),
            ]
    elif anc.get("eye_x") and abs(side) == 2 and len(anc["eye_x"]) >= 1:
        ex = (
            anc["eye_x"][0]
            if len(anc["eye_x"]) == 1
            else (min(anc["eye_x"]) if side < 0 else max(anc["eye_x"]))
        )
        eyes = [(ex, ey, 0.8, 1.0)]
    if eyewear_box is not None and abs(side) < 2:
        ex0, ey0, ex1, ey1 = eyewear_box
        yc = (ey0 + ey1) / 2
        eyes = [
            (ex0 + (ex1 - ex0) * f, yc, e[2], e[3])
            for e, f in zip(sorted(eyes), (0.27, 0.73))
        ]
    eye_boxes: list[Box] = []
    for x, y, _sx, _sc in eyes:
        eye_boxes.append([x - r * 1.2, y - r * 1.3, x + r * 1.2, y + r * 1.3])
    line = pal["line"]
    # ---- eyes
    if eyes_mode == "draw":
        for x, y, sx, sc in eyes:
            rr = r * sc
            if expr == "asleep":
                g.append(
                    path(
                        f"M{x - rr:.1f},{y:.1f} Q{x:.1f},{y + rr * 0.9:.1f} {x + rr:.1f},{y:.1f}",
                        fill="none",
                        stroke=line,
                        stroke_width=lw,
                        stroke_linecap="round",
                    )
                )
                parts.append("eye-closed")
                continue
            if expr == "laugh":
                g.append(
                    path(
                        f"M{x - rr:.1f},{y + rr * 0.3:.1f} Q{x:.1f},{y - rr * 0.9:.1f} "
                        f"{x + rr:.1f},{y + rr * 0.3:.1f}",
                        fill="none",
                        stroke=line,
                        stroke_width=lw * 1.2,
                        stroke_linecap="round",
                    )
                )
                parts.append("eye-happy")
                continue
            big = 1.25 if expr == "surprise" else 1.0
            if human:
                rx, ry = rr * 0.42 * sx * big, rr * 0.56 * big
                if expr == "focused":
                    ry *= 0.6
                ex_, ey_ = x + gx * rr * 0.18, y + gy * rr * 0.15
                g.append(
                    element("ellipse", cx=ex_, cy=ey_, rx=rx, ry=ry, fill=pal["eye"])
                )
                g.append(
                    element(
                        "circle",
                        cx=ex_ - rx * 0.3,
                        cy=ey_ - ry * 0.35,
                        r=max(0.6, rx * 0.38),
                        fill="#ffffff",
                    )
                )
                if expr in ("focused", "angry"):
                    g.append(
                        path(
                            f"M{ex_ - rx * 1.5:.1f},{ey_ - ry * 0.55:.1f} "
                            f"L{ex_ + rx * 1.5:.1f},{ey_ - ry * 0.55:.1f}",
                            fill="none",
                            stroke=line,
                            stroke_width=lw * 0.9,
                            stroke_linecap="round",
                        )
                    )
                parts.append("eye-bead")
            elif sp == "feline":
                rx, ry = rr * 0.95 * sx * big, rr * 1.05 * big
                if expr == "focused":
                    ry *= 0.6
                g.append(
                    element(
                        "ellipse",
                        cx=x,
                        cy=y,
                        rx=rx,
                        ry=ry,
                        fill=pal["iris_cat"],
                        stroke=line,
                        stroke_width=lw * 0.8,
                    )
                )
                g.append(
                    element(
                        "ellipse",
                        cx=x + gx * rx * 0.25,
                        cy=y + gy * ry * 0.2,
                        rx=max(0.8, rx * 0.28),
                        ry=ry * 0.8,
                        fill="#151010",
                    )
                )
                g.append(
                    element(
                        "circle",
                        cx=x - rx * 0.3,
                        cy=y - ry * 0.4,
                        r=max(0.6, rx * 0.22),
                        fill="#ffffff",
                    )
                )
                parts.append("eye-cat")
            else:
                rx, ry = rr * 0.75 * sx * big, rr * 0.85 * big
                if expr == "focused":
                    ry *= 0.6
                if sp == "bird":
                    g.append(
                        element(
                            "circle",
                            cx=x,
                            cy=y,
                            r=rr * 1.05,
                            fill=pal["white"],
                            stroke=line,
                            stroke_width=lw * 0.6,
                        )
                    )
                g.append(
                    element(
                        "ellipse",
                        cx=x + gx * rx * 0.2,
                        cy=y + gy * ry * 0.2,
                        rx=rx,
                        ry=ry,
                        fill=pal["eye"],
                    )
                )
                g.append(
                    element(
                        "circle",
                        cx=x + gx * rx * 0.2 - rx * 0.32,
                        cy=y + gy * ry * 0.2 - ry * 0.38,
                        r=max(0.6, rx * 0.3),
                        fill="#ffffff",
                    )
                )
                parts.append("eye-bead")
    # ---- brows (people always; animals only when the expression needs them)
    if eyes_mode != "none-all" and (human or expr in ("angry", "sad", "surprise")):
        for x, y, sx, sc in eyes:
            rr = r * sc
            by = (
                y
                - rr * (1.55 if human else 1.45)
                - (rr * 0.4 if expr == "surprise" else 0)
            )
            # +1: the inner end is to the right.
            inner = 1 if x < (axis if abs(side) < 2 else x + 1) else -1
            tilt = {"angry": 0.55, "sad": -0.45, "focused": 0.25 if human else 0.0}.get(
                expr, 0.0
            ) * rr
            xa, xb = x - rr * 1.05 * sx, x + rr * 1.05 * sx
            ya = by + (tilt if inner == -1 else -tilt)
            yb = by + (tilt if inner == 1 else -tilt)
            arch = -0.25 * rr if expr not in ("angry",) else 0.05 * rr
            col = pal["brow_old"] if age == "old" else line
            g.append(
                path(
                    f"M{xa:.1f},{ya:.1f} Q{x:.1f},{by + arch:.1f} {xb:.1f},{yb:.1f}",
                    fill="none",
                    stroke=col,
                    stroke_width=lw * (1.5 if human else 1.1),
                    stroke_linecap="round",
                )
            )
        parts.append("brows")
    # ---- old age: lines at the outer eye corners and one on the forehead
    if age == "old" and human:
        for x, y, _sx, _sc in eyes:
            out = -1 if x < (axis if abs(side) < 2 else x - 1) else 1
            ox = x + out * r * 1.25
            g.append(
                path(
                    f"M{ox:.1f},{y - r * 0.15:.1f} l{out * r * 0.45:.1f},{-r * 0.25:.1f} "
                    f"M{ox:.1f},{y + r * 0.2:.1f} l{out * r * 0.45:.1f},{r * 0.2:.1f}",
                    fill="none",
                    stroke=pal["old"],
                    stroke_width=lw * 0.6,
                    stroke_linecap="round",
                )
            )
        fy = eyes[0][1] - r * 2.6
        fx0, fx1 = plate.row(fy)
        if fx1 - fx0 > 2 * r:
            m = (fx0 + fx1) / 2 + (side * 0.15 * half if abs(side) == 1 else 0)
            g.append(
                path(
                    f"M{m - r * 1.3:.1f},{fy:.1f} Q{m:.1f},{fy - r * 0.25:.1f} "
                    f"{m + r * 1.3:.1f},{fy:.1f}",
                    fill="none",
                    stroke=pal["old"],
                    stroke_width=lw * 0.6,
                    stroke_linecap="round",
                    opacity=0.8,
                )
            )
        parts.append("age-lines")
    # ---- cheeks
    if human and (expr in ("smile", "laugh") or age == "child") and abs(side) < 2:
        for x, y, sx, _sc in eyes:
            g.append(
                element(
                    "ellipse",
                    cx=x + (x - axis) * 0.15,
                    cy=y + r * 1.7,
                    rx=r * 0.95 * sx,
                    ry=r * 0.55,
                    fill=pal["cheek"],
                    opacity=0.45,
                )
            )
        parts.append("cheeks")
    # ---- nose, muzzle, beak
    my_share = {"human": 0.79, "bird": 0.62}.get(sp, 0.78) + lower_eyes * 0.5
    ny = top + {"human": 0.63, "bird": 0.56}.get(sp, 0.62) * H + lower_eyes * 0.5 * H
    my = top + my_share * H
    if abs(side) == 2:
        rl, rr_ = plate.row(ny)
        nx = rl if side < 0 else rr_
        mx = (plate.row(my)[0] if side < 0 else plate.row(my)[1]) - (
            side // 2
        ) * 0.12 * W
    else:
        nx = axis + side * 0.05 * half
        mx = axis + side * 0.08 * half
    mw = (0.30 if human else 0.26) * W * (0.8 if abs(side) == 1 else 1.0)
    if abs(side) == 2:
        mw *= 0.45
    if human:
        if abs(side) == 2:
            d1 = side // 2
            # A filled bump of the plate's own colour overlapping the edge (so it
            # stays attached), outlined on its outer side only.
            g.append(
                path(
                    f"M{nx - d1 * r * 0.9:.1f},{ny - r * 1.5:.1f} "
                    f"L{nx - d1 * r * 0.1:.1f},{ny - r * 1.5:.1f} "
                    f"Q{nx + d1 * r * 1.15:.1f},{ny + r * 0.05:.1f} "
                    f"{nx - d1 * r * 0.1:.1f},{ny + r * 0.55:.1f} "
                    f"L{nx - d1 * r * 0.9:.1f},{ny + r * 0.55:.1f} Z",
                    fill=pal["skin"],
                )
            )
            g.append(
                path(
                    f"M{nx - d1 * r * 0.1:.1f},{ny - r * 1.5:.1f} "
                    f"Q{nx + d1 * r * 1.15:.1f},{ny + r * 0.05:.1f} "
                    f"{nx - d1 * r * 0.1:.1f},{ny + r * 0.55:.1f}",
                    fill="none",
                    stroke=line,
                    stroke_width=lw,
                    stroke_linecap="round",
                )
            )
        else:
            d1 = side if side else 1
            g.append(
                path(
                    f"M{nx:.1f},{ny - r * 1.0:.1f} Q{nx + d1 * r * 0.55:.1f},{ny + r * 0.25:.1f} "
                    f"{nx - d1 * r * 0.25:.1f},{ny + r * 0.35:.1f}",
                    fill="none",
                    stroke=line,
                    stroke_width=lw * 0.85,
                    stroke_linecap="round",
                )
            )
        parts.append("nose")
    elif sp in ("canine", "ursine", "bovine", "ovine", "other"):
        mzw = {
            "canine": 0.46,
            "ursine": 0.44,
            "bovine": 0.58,
            "ovine": 0.36,
            "other": 0.38,
        }[sp] * W
        mzh = {
            "canine": 0.28,
            "ursine": 0.30,
            "bovine": 0.30,
            "ovine": 0.24,
            "other": 0.26,
        }[sp] * H
        mcx = nx if abs(side) < 2 else axis - (side // 2) * mzw * 0.35
        mcy = top + 0.70 * H
        if sp != "ovine":
            g.append(
                element(
                    "ellipse",
                    cx=mcx,
                    cy=mcy,
                    rx=mzw / 2 * (0.85 if abs(side) == 1 else 1.0),
                    ry=mzh / 2,
                    fill=pal["pink"] if sp == "bovine" else pal["muzzle"],
                    stroke=line,
                    stroke_width=lw * 0.6,
                    stroke_opacity=0.5,
                )
            )
        if sp == "bovine":
            for k in (-1, 1):
                if abs(side) == 2 and k == side // 2 * -1:
                    continue
                g.append(
                    element(
                        "ellipse",
                        cx=mcx + k * mzw * 0.2,
                        cy=mcy - mzh * 0.05,
                        rx=mzw * 0.07,
                        ry=mzh * 0.13,
                        fill=pal["nose"],
                    )
                )
            ny = mcy - mzh * 0.05
            my = mcy + mzh * 0.32
        else:
            nw = mzw * (0.32 if sp != "ovine" else 0.35)
            nyy = mcy - mzh * 0.32
            nxx = mcx if abs(side) < 2 else axis - (side // 2) * mzw * 0.05
            if sp == "ovine":
                g.append(
                    path(
                        f"M{nxx - nw / 2:.1f},{nyy:.1f} L{nxx:.1f},{nyy + nw * 0.45:.1f} "
                        f"L{nxx + nw / 2:.1f},{nyy:.1f} M{nxx:.1f},{nyy + nw * 0.45:.1f} "
                        f"L{nxx:.1f},{nyy + nw * 0.95:.1f}",
                        fill="none",
                        stroke=line,
                        stroke_width=lw,
                        stroke_linecap="round",
                        stroke_linejoin="round",
                    )
                )
            else:
                g.append(
                    path(
                        f"M{nxx - nw / 2:.1f},{nyy - nw * 0.12:.1f} "
                        f"Q{nxx:.1f},{nyy - nw * 0.42:.1f} {nxx + nw / 2:.1f},{nyy - nw * 0.12:.1f} "
                        f"Q{nxx + nw * 0.3:.1f},{nyy + nw * 0.42:.1f} {nxx:.1f},{nyy + nw * 0.42:.1f} "
                        f"Q{nxx - nw * 0.3:.1f},{nyy + nw * 0.42:.1f} "
                        f"{nxx - nw / 2:.1f},{nyy - nw * 0.12:.1f} Z",
                        fill=pal["nose"],
                    )
                )
                g.append(
                    element(
                        "ellipse",
                        cx=nxx - nw * 0.12,
                        cy=nyy - nw * 0.1,
                        rx=nw * 0.12,
                        ry=nw * 0.07,
                        fill="#ffffff",
                        opacity=0.5,
                    )
                )
            ny = nyy + nw * 0.42
            my = mcy + mzh * 0.22
            mx = nxx
        parts.append("muzzle")
    elif sp == "porcine":
        scx = nx if abs(side) < 2 else axis - (side // 2) * 0.06 * W
        scy = top + 0.66 * H
        srx = (
            0.17
            * W
            * (0.75 if abs(side) == 1 else 1.0)
            * (0.55 if abs(side) == 2 else 1.0)
        )
        sry = 0.12 * H
        g.append(
            element(
                "ellipse",
                cx=scx,
                cy=scy,
                rx=srx,
                ry=sry,
                fill=pal["pink"],
                stroke=line,
                stroke_width=lw * 0.8,
            )
        )
        for k in (-1, 1):
            if abs(side) == 2 and k != side // 2:
                continue
            g.append(
                element(
                    "ellipse",
                    cx=scx + k * srx * 0.4 * (0.4 if abs(side) == 2 else 1),
                    cy=scy,
                    rx=srx * 0.13,
                    ry=sry * 0.32,
                    fill=pal["nose"],
                )
            )
        my = scy + sry + 0.08 * H
        mx = scx
        parts.append("snout")
    elif sp in ("feline", "lagomorph"):
        nw = 0.13 * W
        nxx = nx if abs(side) < 2 else axis - (side // 2) * 0.03 * W
        g.append(
            path(
                f"M{nxx - nw / 2:.1f},{ny - nw * 0.25:.1f} L{nxx + nw / 2:.1f},{ny - nw * 0.25:.1f} "
                f"L{nxx:.1f},{ny + nw * 0.3:.1f} Z",
                fill=pal["pink"],
                stroke=line,
                stroke_width=lw * 0.6,
                stroke_linejoin="round",
            )
        )
        g.append(
            path(
                f"M{nxx:.1f},{ny + nw * 0.3:.1f} L{nxx:.1f},{ny + nw * 0.75:.1f}",
                fill="none",
                stroke=line,
                stroke_width=lw * 0.8,
                stroke_linecap="round",
            )
        )
        if abs(side) < 2:
            for k in (-1, 1):
                for j, dy in enumerate((-0.12, 0.08, 0.28)):
                    x0 = nxx + k * 0.16 * W
                    g.append(
                        path(
                            f"M{x0:.1f},{ny + dy * nw * 3:.1f} "
                            f"l{k * 0.30 * W:.1f},{(j - 1) * 0.06 * H:.1f}",
                            fill="none",
                            stroke=line,
                            stroke_width=lw * 0.45,
                            stroke_linecap="round",
                            opacity=0.75,
                        )
                    )
        else:
            for j in range(3):
                g.append(
                    path(
                        f"M{nxx - (side // 2) * 0.10 * W:.1f},{ny + nw * 0.2:.1f} "
                        f"l{-(side // 2) * 0.32 * W:.1f},{(j - 1) * 0.07 * H:.1f}",
                        fill="none",
                        stroke=line,
                        stroke_width=lw * 0.45,
                        stroke_linecap="round",
                        opacity=0.75,
                    )
                )
        my = ny + nw * 0.75
        mx = nxx
        parts.append("nose-whiskers")
    elif sp == "bird":
        bw = 0.22 * W
        if abs(side) == 2:
            d1 = side // 2
            g.append(
                path(
                    f"M{axis - d1 * 0.06 * W:.1f},{ny - bw * 0.3:.1f} L{axis + d1 * bw:.1f},{ny:.1f} "
                    f"L{axis - d1 * 0.06 * W:.1f},{ny + bw * 0.3:.1f} Z",
                    fill=pal["beak"],
                    stroke=line,
                    stroke_width=lw * 0.7,
                    stroke_linejoin="round",
                )
            )
        else:
            g.append(
                path(
                    f"M{nx - bw / 2:.1f},{ny:.1f} L{nx + bw / 2:.1f},{ny:.1f} "
                    f"L{nx:.1f},{ny + bw * 0.75:.1f} Z",
                    fill=pal["beak"],
                    stroke=line,
                    stroke_width=lw * 0.7,
                    stroke_linejoin="round",
                )
            )
        parts.append("beak")
    # ---- mouth (birds: none, the beak is the mouth)
    if sp != "bird":
        _mouth(g, parts, pal, expr, sp, human, side, mx, my, mw, lw)
    return eye_boxes, parts


def _mouth(
    g: ET.Element,
    parts: list[str],
    pal: dict[str, str],
    expr: str,
    sp: str,
    human: bool,
    side: int,
    mx: Any,
    my: Any,
    mw: Any,
    lw: float,
) -> None:
    line = pal["line"]
    x0, x1 = mx - mw / 2, mx + mw / 2
    if abs(side) == 2:
        d = side // 2
        x0, x1 = (mx, mx - d * mw) if d < 0 else (mx - mw, mx)
        x0, x1 = min(x0, x1), max(x0, x1)
    animal_w = (
        sp in ("feline", "lagomorph", "canine", "ursine", "ovine") and abs(side) < 2
    )
    if expr in ("laugh",) or (expr == "smile" and sp == "canine"):
        dep = mw * (0.55 if expr == "laugh" else 0.4)
        g.append(
            path(
                f"M{x0:.1f},{my:.1f} Q{(x0 + x1) / 2:.1f},{my + dep * 1.45:.1f} {x1:.1f},{my:.1f} Z",
                fill=pal["mouth"],
                stroke=line,
                stroke_width=lw * 0.8,
                stroke_linejoin="round",
            )
        )
        g.append(
            element(
                "ellipse",
                cx=(x0 + x1) / 2,
                cy=my + dep * 0.52,
                rx=(x1 - x0) * 0.22,
                ry=dep * 0.18,
                fill=pal["tongue"],
            )
        )
        if human:
            g.append(
                path(
                    f"M{x0 + mw * 0.12:.1f},{my + lw * 0.4:.1f} L{x1 - mw * 0.12:.1f},{my + lw * 0.4:.1f} "
                    f"L{x1 - mw * 0.16:.1f},{my + dep * 0.22:.1f} L{x0 + mw * 0.16:.1f},{my + dep * 0.22:.1f} Z",
                    fill="#ffffff",
                )
            )
        parts.append("mouth-open")
    elif expr == "surprise":
        g.append(
            element(
                "ellipse",
                cx=(x0 + x1) / 2,
                cy=my + mw * 0.08,
                rx=mw * 0.16,
                ry=mw * 0.22,
                fill=pal["mouth"],
                stroke=line,
                stroke_width=lw * 0.7,
            )
        )
        parts.append("mouth-o")
    elif animal_w and expr in ("neutral", "smile", "focused", "asleep"):
        c = (x0 + x1) / 2
        s = 0.18 if expr == "smile" else 0.08
        g.append(
            path(
                f"M{c - mw * 0.4:.1f},{my - mw * 0.05:.1f} Q{c - mw * 0.2:.1f},{my + mw * s * 1.6:.1f} "
                f"{c:.1f},{my - mw * 0.05:.1f} Q{c + mw * 0.2:.1f},{my + mw * s * 1.6:.1f} "
                f"{c + mw * 0.4:.1f},{my - mw * 0.05:.1f}",
                fill="none",
                stroke=line,
                stroke_width=lw * 0.9,
                stroke_linecap="round",
            )
        )
        parts.append("mouth-w")
    else:
        sag = {
            "neutral": 0.06,
            "smile": 0.28,
            "sad": -0.22,
            "angry": -0.14,
            "asleep": 0.04,
            "focused": 0.0,
        }.get(expr, 0.06) * mw
        if expr in ("focused", "asleep", "sad", "angry"):
            x0, x1 = (x0 + mw * 0.15, x1 - mw * 0.15)
        if abs(side) == 2:
            sag *= 0.6
        midy = my + sag * 2 * 0.5
        if expr == "angry" and human:
            g.append(
                path(
                    f"M{x0:.1f},{my:.1f} Q{(x0 + x1) / 2:.1f},{my - mw * 0.16:.1f} {x1:.1f},{my:.1f} "
                    f"Q{(x0 + x1) / 2:.1f},{my + mw * 0.12:.1f} {x0:.1f},{my:.1f} Z",
                    fill=pal["mouth"],
                    stroke=line,
                    stroke_width=lw * 0.8,
                    stroke_linejoin="round",
                )
            )
            g.append(
                path(
                    f"M{x0 + mw * 0.1:.1f},{my - mw * 0.02:.1f} L{x1 - mw * 0.1:.1f},{my - mw * 0.02:.1f}",
                    fill="none",
                    stroke="#ffffff",
                    stroke_width=lw * 0.8,
                )
            )
            parts.append("mouth-shout")
        else:
            g.append(
                path(
                    f"M{x0:.1f},{my:.1f} Q{(x0 + x1) / 2:.1f},{midy + sag:.1f} {x1:.1f},{my:.1f}",
                    fill="none",
                    stroke=line,
                    stroke_width=lw * 1.05,
                    stroke_linecap="round",
                )
            )
            parts.append("mouth-line")


# ---------------------------------------------------------------- the painter's declarations
def attrs_of(head: ET.Element) -> tuple[dict[str, str], list[str], list[str]]:
    """(attributes, invalid required ones, defaulted optional ones)."""
    raw = {
        "facing": head.get("data-facing"),
        "expression": head.get("data-expression"),
        "gaze": head.get("data-gaze"),
        "species": head.get("data-species"),
        "age": head.get("data-age"),
    }
    lists = {
        "facing": FACINGS,
        "expression": EXPRESSIONS,
        "gaze": GAZES,
        "species": SPECIES,
        "age": AGES,
    }
    defaults = {"expression": "neutral", "gaze": "forward", "age": "adult"}
    bad: list[str] = []
    defaulted: list[str] = []
    out: dict[str, str] = {}
    for k, given in raw.items():
        v = (given or "").strip().lower()
        if v not in lists[k]:
            if k in defaults:
                defaulted.append(f"{k}={given}")
                v = defaults[k]
            else:
                bad.append(f"{k}={given}")
        out[k] = v
    return out, bad, defaulted


def wearing_ids(ck: dict[str, Any] | None, base: str) -> list[str]:
    """Ids of things the checklist says the character wears, or objects it has on."""
    thing = re.sub(r"-\d+$", "", base)
    kinds = {t["id"]: t["kind"] for t in (ck or {}).get("things", [])}
    return [
        r["a"]
        for r in (ck or {}).get("relations", [])
        if r.get("type") in ("wearing", "on")
        and r.get("b") == thing
        and (r.get("type") == "wearing" or kinds.get(r["a"]) == "object")
    ]


def anchors_geo(features: list[dict[str, Any]], plate: Plate) -> dict[str, Any]:
    """Where the painter put its eyes and mouth, read from the geometry of the
    feature shapes: the highest pair of compact blobs side by side on the plate
    is the eye pair; the widest flat shape under it, near the axis, the mouth;
    a single dark blob is a one-eye (profile) head."""
    x0, y0, x1, y1 = plate.left, plate.top, plate.right, plate.bottom
    W, H = x1 - x0, y1 - y0
    pa = W * H
    c: list[dict[str, Any]] = []
    for fi in features:
        b = fi["box"]
        if not b:
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        c.append({**fi, "w": w, "h": h, "cx": cx, "cy": cy})
    blobs = [
        k
        for k in c
        if 0.0015 * pa <= k["w"] * k["h"] <= 0.06 * pa
        and 0.33 <= k["w"] / max(1e-6, k["h"]) <= 3.0
        and x0 - 0.05 * W <= k["cx"] <= x1 + 0.05 * W
        and y0 + 0.12 * H <= k["cy"] <= y0 + 0.72 * H
    ]
    best: tuple[Any, dict[str, Any], dict[str, Any]] | None = None
    for i in range(len(blobs)):
        for j in range(i + 1, len(blobs)):
            a, b = blobs[i], blobs[j]
            if (
                abs(a["cy"] - b["cy"]) < 0.08 * H
                and 0.12 * W < abs(a["cx"] - b["cx"]) < 0.75 * W
                and 0.4 < (a["w"] * a["h"]) / max(1e-6, b["w"] * b["h"]) < 2.5
            ):
                y = (a["cy"] + b["cy"]) / 2
                if best is None or y < best[0] - 0.02 * H:
                    best = (y, a, b)
    out: dict[str, Any] = {}
    if best:
        out["eye_y"] = best[0]
        out["eye_x"] = [best[1]["cx"], best[2]["cx"]]
        mid = sum(out["eye_x"]) / 2
        mouths = [
            k
            for k in c
            if k["cy"] > best[0] + 0.12 * H
            and k["w"] > 0.10 * W
            and k["w"] / max(1e-6, k["h"]) > 1.2
            and abs(k["cx"] - mid) < 0.15 * W
            and k["cy"] < y1 + 0.05 * H
        ]
        if mouths:
            out["mouth_y"] = max(mouths, key=lambda k: k["w"])["cy"]
    else:
        dark = [k for k in blobs if k["lum"] < 110 and k["w"] * k["h"] <= 0.03 * pa]
        if len(dark) == 1:
            out = {"eye_y": dark[0]["cy"], "eye_x": [dark[0]["cx"]], "single_eye": True}
    return out


# ---------------------------------------------------------------- one picture
def apply(
    svg: str, ck: dict[str, Any] | None = None, raise_hats: bool = True
) -> tuple[str, list[dict[str, Any]]]:
    """The picture with its declared faces redrawn, and one report per head
    (decision: kit, back or fallback, with its reason and geometry)."""
    doc = Doc(svg)
    report: list[dict[str, Any]] = []
    # Deferred tree edits: (kind, node, payload).
    edits: list[tuple[str, ET.Element, Any]] = []
    for base, head in heads_of(doc):
        rec: dict[str, Any] = {"head": head.get("id"), "name": base}
        a, bad, defaulted = attrs_of(head)
        rec["attrs"], rec["defaulted"] = a, defaulted
        if bad:
            rec.update(
                decision="fallback",
                reason="invalid or missing attribute: " + ", ".join(bad),
            )
            report.append(rec)
            continue
        face = find_named(doc, base, "face", head)
        if face is None:
            rec.update(decision="fallback", reason="no face group")
            report.append(rec)
            continue
        if face is head:
            rec.update(decision="fallback", reason="face group is the head group")
            report.append(rec)
            continue
        if a["facing"] == "back":
            rec.update(
                decision="back",
                reason="seen from the back: nothing to draw, the model's head kept",
            )
            report.append(rec)
            continue
        hats = [n for n in [find_named(doc, base, "hat", head)] if n is not None]
        for wid in wearing_ids(ck, base):
            n = doc.by_id(wid)
            if (
                n is not None
                and n not in hats
                and n is not head
                and head not in doc.ancestors(n)
            ):
                hats.append(n)
        eyewear = find_named(doc, base, "eyewear", head)
        if any(
            face in doc.ancestors(n)
            for n in hats + ([eyewear] if eyewear is not None else [])
        ):
            rec.update(
                decision="fallback", reason="hat or eyewear inside the face group"
            )
            report.append(rec)
            continue
        covers = [
            n
            for n in doc.root.iter()
            if (n.get("data-covers") or "").strip().lower() == "eyes"
            and (n in hats or n is eyewear or n.get("id", "").startswith(base + "-"))
        ]
        # Any other group declared over the eyes and touching this head counts as
        # a covering hat.
        hb0 = doc.box(doc.shapes_under(head))
        for n in doc.root.iter():
            if (
                (n.get("data-covers") or "").strip().lower() == "eyes"
                and n not in covers
                and n is not head
                and head not in doc.ancestors(n)
                and n not in doc.ancestors(head)
            ):
                b = doc.box(doc.shapes_under(n))
                if (
                    b
                    and hb0
                    and b[0] < hb0[2]
                    and b[2] > hb0[0]
                    and b[1] < hb0[3]
                    and b[3] > hb0[1]
                ):
                    covers.append(n)
                    if n is not eyewear and n not in hats:
                        hats.append(n)
        excluded = [face] + hats + ([eyewear] if eyewear is not None else [])
        hshapes = doc.shapes_under(head, exclude=excluded)
        fshapes_all = doc.shapes_under(face)
        if not fshapes_all:
            rec.update(decision="fallback", reason="empty face group")
            report.append(rec)
            continue
        hbox = doc.box(hshapes + fshapes_all)
        if hbox is None or (hbox[2] - hbox[0]) < 0.012 * doc.vb[2]:
            rec.update(
                decision="fallback", reason="head too small or invisible", box=hbox
            )
            report.append(rec)
            continue
        pad = 0.08 * max(hbox[2] - hbox[0], hbox[3] - hbox[1])
        region = (hbox[0] - pad, hbox[1] - pad, hbox[2] + pad, hbox[3] + pad)
        px = 320
        scale = (region[2] - region[0]) / px
        # Shapes of the face group: the large ones are the face plate the
        # painter put there (kept), the others are features (hidden).
        all_area = int(
            (doc.render(hshapes + fshapes_all, region, px)[..., 3] >= 128).sum()
        )
        finfo: list[dict[str, Any]] = []
        for f in fshapes_all:
            rgba = doc.render([f], region, px)
            al = rgba[..., 3] >= 100
            n_px = int(al.sum())
            ys_, xs_ = np.nonzero(al)
            bx = (
                [
                    region[0] + xs_.min() * scale,
                    region[1] + ys_.min() * scale,
                    region[0] + (xs_.max() + 1) * scale,
                    region[1] + (ys_.max() + 1) * scale,
                ]
                if n_px
                else None
            )
            finfo.append(
                {
                    "node": f,
                    "area": n_px,
                    "box": bx,
                    "lum": float(lum(list(rgba[al][:, :3].mean(axis=0))))
                    if n_px
                    else 255.0,
                    "filled": (f.get("fill") or "").strip().lower() != "none",
                }
            )
        plate_like = [
            fi["node"] for fi in finfo if fi["area"] > 0.25 * max(1, all_area)
        ]
        features = [fi for fi in finfo if fi["node"] not in plate_like]
        rec["kept_from_face"] = len(plate_like)
        if not features:
            rec.update(decision="fallback", reason="face group holds no feature")
            report.append(rec)
            continue
        hshapes = hshapes + plate_like
        if not hshapes:
            rec.update(
                decision="fallback", reason="head group holds nothing but the features"
            )
            report.append(rec)
            continue
        alphas = [doc.render([s], region, px)[..., 3] >= 128 for s in hshapes]
        top = np.full(alphas[0].shape, -1)
        for k, al in enumerate(alphas):
            top[al] = k
        areas = [(int((top == k).sum()), k) for k in range(len(hshapes))]
        areas.sort(reverse=True)
        area, k = areas[0]
        if area < 150:
            rec.update(
                decision="fallback",
                reason="no face plate (largest visible shape under 150 px of 320)",
            )
            report.append(rec)
            continue
        vis = ndi.binary_opening(np.asarray(top == k), iterations=2)
        lab, nlab = ndi.label(vis)
        if nlab > 1:
            sizes = ndi.label_sums(vis, lab, nlab)
            vis = np.asarray(lab == (int(np.argmax(sizes)) + 1))
        whole = doc.render(hshapes, region, px)
        col = (
            whole[vis][:, :3].mean(axis=0)
            if vis.any()
            else np.array([200.0, 160.0, 130.0])
        )
        ys, xs = np.nonzero(vis)
        ptop, pbot = region[1] + ys.min() * scale, region[1] + (ys.max() + 1) * scale
        pleft, pright = region[0] + xs.min() * scale, region[0] + (xs.max() + 1) * scale
        rows: dict[int, tuple[Any, Any]] = {}
        for y in range(vis.shape[0]):
            xx = np.nonzero(vis[y])[0]
            if len(xx):
                rows[y] = (xx.min(), xx.max() + 1)
        plate = Plate(
            ptop, pbot, pleft, pright, _row_reader(rows, region, scale, pleft, pright)
        )
        rec["plate"] = [round(v, 1) for v in (pleft, ptop, pright, pbot)]
        rec["hbox"] = [round(float(v), 1) for v in hbox]
        hb = [doc.box(doc.shapes_under(h)) for h in hats]
        rec["hat_boxes"] = [[round(float(v), 1) for v in b] for b in hb if b]
        rec["plate_rgb"] = [int(v) for v in col]
        rec["plate_share_of_head"] = round(
            area / max(1, int((whole[..., 3] >= 128).sum())), 3
        )
        pal = palette(list(col))
        plate.anchors = anchors_geo(features, plate)
        rec["anchors"] = {
            k: (v if not isinstance(v, float) else round(v, 1))
            for k, v in plate.anchors.items()
        }
        # Eyewear: align the eyes on it; draw none under eyewear covering them.
        ew_box = doc.box(doc.shapes_under(eyewear)) if eyewear is not None else None
        eyes_mode = "draw"
        if eyewear is not None and eyewear in covers:
            eyes_mode = "none"
        if any(h in covers for h in hats):
            rec["hat_covers_eyes_by_request"] = True
        # The hat rule: dry run of the eye boxes, then the hat's mask over them.
        dry = ET.Element(Q + "g")
        eye_boxes, _ = draw_face(dry, plate, a, pal, eyes_mode, ew_box)
        lift, lower = 0.0, 0.0
        hat_info: list[dict[str, Any]] = []
        for hat in hats:
            hs = doc.shapes_under(hat)
            if not hs or hat in covers:
                continue
            reg2 = (
                region[0],
                region[1] - (region[3] - region[1]) * 0.6,
                region[2],
                region[3],
            )
            hal = doc.render(hs, reg2, px)[..., 3] >= 128
            sc2 = (reg2[2] - reg2[0]) / px
            need = 0.0
            for x0, y0, x1, y1 in eye_boxes:
                c0, c1 = (
                    max(0, int((x0 - reg2[0]) / sc2)),
                    max(0, int((x1 - reg2[0]) / sc2)),
                )
                r0, r1 = (
                    max(0, int((y0 - reg2[1]) / sc2)),
                    max(0, int((y1 - reg2[1]) / sc2)),
                )
                sub = hal[r0:r1, c0:c1]
                if sub.size and sub.mean() > 0.04:
                    yy = np.nonzero(sub.any(axis=1))[0]
                    bottom = reg2[1] + (r0 + yy.max() + 1) * sc2
                    need = max(need, bottom - y0)
            if need > 0:
                need = need + 0.15 * (eye_boxes[0][3] - eye_boxes[0][1])
                hat_info.append({"hat": hat.get("id"), "overlap": round(need, 1)})
                lift = max(lift, need)
        Hp = pbot - ptop
        if lift > 0 and raise_hats:
            cap = 0.30 * Hp
            if lift > cap:
                lower = min(0.08, (lift - cap) / Hp)
                lift = cap
            rec["hat_lift"] = round(lift, 1)
            rec["eyes_lowered"] = round(lower, 3)
            rec["hats"] = hat_info
            for hat in hats:
                if hat in covers:
                    continue
                edits.append(("lift", hat, lift))
        elif lift > 0:
            rec["hat_over_eyes_kept"] = hat_info
        g = ET.Element(Q + "g", {"id": f"{head.get('id')}-kit", "data-kit": "17a"})
        eye_boxes, parts = draw_face(
            g, plate, a, pal, eyes_mode, ew_box, lower_eyes=lower
        )
        rec.update(
            decision="kit",
            parts=parts,
            eyes=eyes_mode,
            eyewear=bool(eyewear is not None),
            n_hats=len(hats),
        )
        edits.append(
            ("face", face, (g, [fi["node"] for fi in features] if plate_like else None))
        )
        report.append(rec)
    _apply_edits(doc, edits)
    return ET.tostring(doc.root, encoding="unicode"), report


def _row_reader(
    rows: dict[int, tuple[Any, Any]],
    region: tuple[float, float, float, float],
    scale: float,
    pleft: float,
    pright: float,
) -> Callable[[float], tuple[float, float]]:
    """row(y): left and right edge of the plate at height y (nearest measured row
    within 40 px, else the plate box)."""

    def row(yu: float) -> tuple[float, float]:
        yi = int((yu - region[1]) / scale)
        for d in range(0, 40):
            for yy in (yi - d, yi + d):
                if yy in rows:
                    x0, x1 = rows[yy]
                    return region[0] + x0 * scale, region[0] + x1 * scale
        return pleft, pright

    return row


def _apply_edits(doc: Doc, edits: list[tuple[str, ET.Element, Any]]) -> None:
    for kind, node, payload in edits:
        par = doc.parent[id(node)]
        if kind == "face":
            g, feats = payload
            if feats is None:
                # The whole face group goes; the kit takes its place.
                node.set("display", "none")
                host, idx = par, list(par).index(node) + 1
            else:
                # The face group keeps the plate the painter drew there; each
                # feature goes; the kit goes last inside it.
                for f in feats:
                    f.set("display", "none")
                host, idx = node, len(node)
            pm = doc.ctm(host, include_self=True) if host is not doc.root else np.eye(3)
            g.set("transform", mstr(np.linalg.inv(pm)))
            host.insert(idx, g)
        elif kind == "lift":
            if node.get("data-kit-lifted"):
                continue
            pm = doc.ctm(par, include_self=True) if par is not doc.root else np.eye(3)
            t = np.eye(3)
            t[1, 2] = -payload
            wrap = ET.Element(
                Q + "g",
                {
                    "transform": mstr(np.linalg.inv(pm) @ t @ pm),
                    "data-kit-lift": f"{payload:.1f}",
                },
            )
            node.set("data-kit-lifted", "1")
            idx = list(par).index(node)
            par.remove(node)
            wrap.append(node)
            par.insert(idx, wrap)
            doc.parent[id(node)] = wrap
            doc.parent[id(wrap)] = par
