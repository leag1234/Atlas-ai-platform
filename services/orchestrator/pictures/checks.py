"""Four code checks on the named groups and rendered masks of a free SVG.

- attachment: every named sub-part touches the mass it belongs to; a thing
  rendered alone is one connected matter;
- organic: inside faces and living bodies, the share of straight-edged filled
  primitives (rect, polygon, straight paths);
- depth: of things resting on a common surface, the lower base is nearer; the
  paint order and the overlaps must agree;
- readability: overlap and tangles between characters, limbs of one character
  crossing, lost edges against the surroundings, clutter.

Nothing names a subject; every threshold is a share of the frame or of the
thing measured. The frame is 512 px on its longer side here.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from typing import Any

import numpy as np
import numpy.typing as npt

from services.orchestrator.pictures import ndimage as ndi
from services.orchestrator.pictures.geometry import Box, Mask, norm_id, tag
from services.orchestrator.pictures.judge import LIMBS, Judge

FACE_WORDS = ("head", "face", "tete", "visage")
CARRY = ("inside", "on", "holding", "wearing", "against")

# Thresholds, set on the round 10 pictures and frozen before measurement.
T: dict[str, float] = {
    "tol": 0.004,  # two masks touch within this share of the frame
    "stray_min_px": 6,  # a loose fragment smaller than this is antialiasing
    "part_gap": 0.008,  # a named sub-part farther than this is detached
    "geo_face": 0.02,  # polygon-like shapes inside a head, share of its mass
    "geo_body": 0.15,  # visible share of polygon-like shapes over a living body
    "tie": 0.015,  # two bases closer than this are at the same depth
    "cover_min": 0.03,  # the far thing covers at least this share of the smaller
    "cover_share": 0.25,  # ... and this share of the overlap
    "same_overlap": 0.10,  # same depth sharing this much: the same place
    "size_grow": 1.15,  # a farther instance taller than the nearer by this factor
    "widen": 1.08,  # top face of a support wider far than near by this factor
    "char_overlap": 0.20,  # two characters sharing this much of the smaller one
    "merge_share": 0.10,  # two characters overlapping by this share ...
    "merge_rgb": 40.0,  # ... whose mean colours differ by less merge in one mass
    "knots": 3,  # two characters crossing in this many places are tangled
    "lost_l": 12.0,  # an edge whose sides differ by less than this in value ...
    "lost_rgb": 30.0,  # ... and less than this in colour is a lost edge
    "lost_share": 0.5,  # this share of the outline lost: the subject melts in
    "contrast_l": 18.0,
    "contrast_rgb": 40.0,
    "tiny": 0.012,  # a shape whose longer side is under this share is tiny
    "micro": 0.005,  # ... under this it is smaller than a few pixels
    "fragment": 0.0003,  # a visible piece under this share of the frame area
}
_NUMBER = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_PATH_TOKEN = re.compile(
    r"[MmLlHhVvCcSsQqTtAaZz]|-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
)
_NAMED_SHAPES = ("path", "rect", "circle", "ellipse", "polygon", "polyline", "line")

Point = tuple[float, float]


def effective(
    node: ET.Element | None, parent: dict[int, ET.Element], name: str
) -> str | None:
    """Presentation attribute inherited from the ancestors (attribute or style)."""
    while node is not None:
        value = node.get(name)
        if value is None:
            found = re.search(
                rf"(?:^|;)\s*{name}\s*:\s*([^;]+)", node.get("style") or ""
            )
            value = found.group(1).strip() if found else None
        if value is not None and value != "inherit":
            return value
        node = parent.get(id(node))
    return None


def _arc(
    p0: Point,
    rx: float,
    ry: float,
    phi: float,
    large: bool,
    sweep: bool,
    p1: Point,
    n: int = 12,
) -> list[Point]:
    """Points of an SVG elliptical arc (endpoint form), end point included."""
    if rx == 0 or ry == 0 or p0 == p1:
        return [p1]
    phi = math.radians(phi)
    c, s_ = math.cos(phi), math.sin(phi)
    dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1, y1 = c * dx + s_ * dy, -s_ * dx + c * dy
    rx, ry = abs(rx), abs(ry)
    lam = x1 * x1 / (rx * rx) + y1 * y1 / (ry * ry)
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = max(0.0, rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1)
    den = rx * rx * y1 * y1 + ry * ry * x1 * x1
    k = (math.sqrt(num / den) if den else 0.0) * (-1 if large == sweep else 1)
    cx1, cy1 = k * rx * y1 / ry, -k * ry * x1 / rx
    cx = c * cx1 - s_ * cy1 + (p0[0] + p1[0]) / 2
    cy = s_ * cx1 + c * cy1 + (p0[1] + p1[1]) / 2
    a0 = math.atan2((y1 - cy1) / ry, (x1 - cx1) / rx)
    a1 = math.atan2((-y1 - cy1) / ry, (-x1 - cx1) / rx)
    da = a1 - a0
    if sweep and da < 0:
        da += 2 * math.pi
    if not sweep and da > 0:
        da -= 2 * math.pi
    return [
        (
            cx
            + rx * math.cos(a0 + da * i / n) * c
            - ry * math.sin(a0 + da * i / n) * s_,
            cy
            + rx * math.cos(a0 + da * i / n) * s_
            + ry * math.sin(a0 + da * i / n) * c,
        )
        for i in range(1, n + 1)
    ]


def _bezier(points: list[Point], n: int) -> list[Point]:
    out: list[Point] = []
    for k in range(1, n + 1):
        t = k / n
        pts = list(points)
        while len(pts) > 1:
            pts = [
                ((1 - t) * a[0] + t * b[0], (1 - t) * a[1] + t * b[1])
                for a, b in zip(pts, pts[1:])
            ]
        out.append(pts[0])
    return out


def flatten(d: str | None, n: int = 10) -> list[list[Point]]:
    """Sub-paths of an SVG path as lists of points (curves sampled)."""
    tokens = _PATH_TOKEN.findall(d or "")
    subs: list[list[Point]] = []
    cur: list[Point] = []
    pos: Point = (0.0, 0.0)
    start: Point = (0.0, 0.0)
    last_c: Point | None = None
    cmd: str | None = None
    i = 0
    need = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7}
    try:
        while i < len(tokens):
            tok = tokens[i]
            if tok.isalpha():
                cmd = tok
                i += 1
                if cmd in "Zz":
                    if cur:
                        subs.append(cur)
                    cur, pos = [], start
                    continue
            if cmd is None or cmd in "Zz":
                i += 1
                continue
            up = cmd.upper()
            a = [float(x) for x in tokens[i : i + need[up]]]
            if len(a) < need[up]:
                break
            i += need[up]
            rel = cmd.islower()
            ox, oy = pos if rel else (0.0, 0.0)
            if up == "M":
                if cur:
                    subs.append(cur)
                pos = start = (a[0] + ox, a[1] + oy)
                cur = [pos]
                cmd = "l" if rel else "L"
            elif up == "L":
                pos = (a[0] + ox, a[1] + oy)
                cur.append(pos)
            elif up == "H":
                pos = (a[0] + ox, pos[1])
                cur.append(pos)
            elif up == "V":
                pos = (pos[0], a[0] + oy)
                cur.append(pos)
            elif up in "CS":
                if up == "C":
                    c1 = (a[0] + ox, a[1] + oy)
                    c2, end = (a[2] + ox, a[3] + oy), (a[4] + ox, a[5] + oy)
                else:
                    c1 = (
                        (2 * pos[0] - last_c[0], 2 * pos[1] - last_c[1])
                        if last_c
                        else pos
                    )
                    c2, end = (a[0] + ox, a[1] + oy), (a[2] + ox, a[3] + oy)
                cur += _bezier([pos, c1, c2, end], n)
                pos, last_c = end, c2
                continue
            elif up in "QT":
                if up == "Q":
                    c1, end = (a[0] + ox, a[1] + oy), (a[2] + ox, a[3] + oy)
                else:
                    c1 = (
                        (2 * pos[0] - last_c[0], 2 * pos[1] - last_c[1])
                        if last_c
                        else pos
                    )
                    end = (a[0] + ox, a[1] + oy)
                cur += _bezier([pos, c1, end], n)
                pos, last_c = end, c1
                continue
            elif up == "A":
                end = (a[5] + ox, a[6] + oy)
                cur += _arc(pos, a[0], a[1], a[2], bool(a[3]), bool(a[4]), end)
                pos = end
            last_c = None
    except (ValueError, KeyError, ZeroDivisionError):
        pass
    if cur:
        subs.append(cur)
    return subs


def _rdp(points: list[Point], eps: float) -> list[Point]:
    if len(points) < 3:
        return list(points)
    (x0, y0), (x1, y1) = points[0], points[-1]
    dx, dy = x1 - x0, y1 - y0
    norm = math.hypot(dx, dy) or 1e-9
    best, idx = 0.0, 0
    for k in range(1, len(points) - 1):
        px, py = points[k]
        dist = (
            abs(dy * (px - x0) - dx * (py - y0)) / norm
            if norm > 1e-6
            else math.hypot(px - x0, py - y0)
        )
        if dist > best:
            best, idx = dist, k
    if best <= eps:
        return [points[0], points[-1]]
    return _rdp(points[: idx + 1], eps)[:-1] + _rdp(points[idx:], eps)


def corners(node: ET.Element) -> int | None:
    """Corners of the simplest polygon following the outline of a shape within
    3 % of its diagonal (a triangle 3, a quadrilateral 4, a circle 8 or more);
    None for a shape with no measurable outline."""
    t = tag(node)
    if t == "rect":

        def num(key: str) -> float:
            found = re.match(r"-?[\d.]+", node.get(key) or "")
            return float(found.group(0)) if found else 0.0

        side = min(num("width"), num("height"))
        return 4 if side > 0 and max(num("rx"), num("ry")) < 0.2 * side else 12
    if t in ("circle", "ellipse"):
        return 12
    if t == "polygon":
        vals = [float(v) for v in _NUMBER.findall(node.get("points") or "")]
        subs = [list(zip(vals[0::2], vals[1::2]))]
    elif t == "path":
        subs = flatten(node.get("d"))
    else:
        return None
    subs = [p for p in subs if len(p) >= 3]
    if not subs:
        return None
    pts = max(subs, key=len)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    diag = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
    if diag <= 0:
        return None
    far = max(
        range(len(pts)),
        key=lambda k: math.hypot(pts[k][0] - pts[0][0], pts[k][1] - pts[0][1]),
    )
    if far == 0:
        return None
    eps = 0.03 * diag
    a, b = _rdp(pts[: far + 1], eps), _rdp(pts[far:] + [pts[0]], eps)
    poly = a[:-1] + b[:-1]
    return max(2, len(poly))


def is_straight(node: ET.Element) -> bool:
    """A filled primitive that reads as a polygon: at most 5 corners joined by
    straight edges."""
    n = corners(node)
    return n is not None and 3 <= n <= 5


class Checks:
    def __init__(
        self,
        svg: str,
        ck: dict[str, Any],
        living: dict[str, bool] | None = None,
        frame: int = 512,
    ) -> None:
        self.ck = ck
        self.j = Judge(svg, ck, frame)
        self.g = self.j.g
        self.things = self.j.things
        self.living = living or {}
        self._sm: dict[int, Mask] = {}
        g = self.g
        self.full = g.fw * g.fh
        self.carried = {(r["a"], r["b"]) for r in ck["relations"] if r["type"] in CARRY}

    # ------------------------------------------------------------ helpers
    def px(self, share: float) -> int:
        return max(1, int(round(share * max(self.g.fw, self.g.fh))))

    def units(self, pixels: float | None) -> int:
        if pixels is None:
            raise TypeError("empty_mask")
        return int(round(pixels * self.g.unit))

    def smask(self, node: ET.Element) -> Mask:
        key = id(node)
        if key not in self._sm:
            self._sm[key] = self.g.alpha([node], 128)
        return self._sm[key]

    def shapes(self, name: str) -> list[ET.Element]:
        node = self.g.named.get(name)
        return (
            [s for s in self.g.shapes_of(node) if tag(s) != "text"]
            if node is not None
            else []
        )

    def union(self, nodes: Iterable[ET.Element]) -> Mask:
        m = np.zeros((self.g.ch, self.g.cw), bool)
        for n in nodes:
            m |= self.smask(n)
        return m

    def top_index(
        self, nodes: list[ET.Element]
    ) -> tuple[list[ET.Element], npt.NDArray[np.int32]]:
        """(nodes in painter's order, index of the one showing at each pixel or -1)."""
        nodes = sorted(nodes, key=lambda n: self.g.order[id(n)])
        top = np.full((self.g.ch, self.g.cw), -1, np.int32)
        for k, n in enumerate(nodes):
            top[self.smask(n)] = k
        return nodes, top

    def instances(
        self, kinds: tuple[str, ...], singular_only: bool = False
    ) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for t in self.ck["things"]:
            if t["kind"] not in kinds:
                continue
            if singular_only and t["plural"]:
                continue
            for name in self.j.instances(t["id"]):
                m = self.g.mask(name)
                if self.g.in_frame(m).sum() >= 0.0012 * self.full:
                    out.append((t["id"], name))
        return out

    def named_descendants(self, name: str) -> list[ET.Element]:
        node = self.g.named.get(name)
        out: list[ET.Element] = []
        if node is None:
            return out
        for n in node.iter():
            if (
                n is not node
                and n.get("id")
                and (tag(n) == "g" or tag(n) in _NAMED_SHAPES)
            ):
                out.append(n)
        return out

    def named_parent(self, node: ET.Element, stop: ET.Element) -> ET.Element:
        p = self.g.parent.get(id(node))
        while p is not None and p is not stop and not p.get("id"):
            p = self.g.parent.get(id(p))
        return p if p is not None else stop

    def head_node(self, name: str) -> ET.Element | None:
        """The named sub-group that is the head or face of a character (largest)."""
        best: ET.Element | None = None
        area = 0
        for n in self.named_descendants(name):
            tokens = norm_id(n.get("id")).split("-")
            if any(w in tokens for w in FACE_WORDS):
                a = int(
                    self.union(
                        [s for s in self.g.shapes_of(n) if tag(s) != "text"]
                    ).sum()
                )
                if a > area:
                    best, area = n, a
        return best

    def ubox(self, mask: Mask) -> Box | None:
        return self.g.ubox(mask)

    # ------------------------------------------------------------ a. attachment
    def attachment(self) -> dict[str, Any]:
        g = self.g
        tol = self.px(T["tol"])
        strays: list[dict[str, Any]] = []
        parts: list[dict[str, Any]] = []
        seen = 0
        worn = {
            r["a"]
            for r in self.ck["relations"]
            if r["type"] in ("wearing", "holding")
            and self.things[r["b"]]["kind"] == "character"
        }
        for tid, name in self.instances(("character", "object"), singular_only=True):
            # A character, or what a character wears or holds.
            figure = self.things[tid]["kind"] == "character" or tid in worn
            nodes = self.shapes(name)
            if not nodes:
                continue
            m = self.union(nodes)
            area = int(m.sum())
            if area < 40:
                continue
            seen += 1
            lab, n = ndi.label(ndi.binary_dilation(m, iterations=tol))
            if n > 1:
                sizes = ndi.label_sums(m, lab, n)
                main = int(np.argmax(sizes)) + 1
                core_mask = (lab == main) & m
                for k, size in enumerate(sizes):
                    if k + 1 == main or size < T["stray_min_px"]:
                        continue
                    piece = (lab == k + 1) & m
                    if not g.in_frame(piece).any():
                        continue
                    strays.append(
                        {
                            "thing": name,
                            "figure": figure,
                            "box": self.ubox(piece),
                            "share": round(float(size) / area, 4),
                            "gap": self.units(g.gap(piece, core_mask)),
                        }
                    )
            node = g.named[name]
            for sub in self.named_descendants(name):
                own = [s for s in g.shapes_of(sub) if tag(s) != "text"]
                if not own:
                    continue
                parent = self.named_parent(sub, node)
                own_ids = {id(s) for s in own}
                rest = [
                    s
                    for s in g.shapes_of(parent)
                    if tag(s) != "text" and id(s) not in own_ids
                ]
                if not rest:
                    continue
                dm, rm = self.union(own), self.union(rest)
                if dm.sum() < T["stray_min_px"] or rm.sum() < 40:
                    continue
                gap = g.gap(dm, rm)
                if gap is not None and gap > self.px(T["part_gap"]):
                    parts.append(
                        {
                            "thing": name,
                            "figure": figure,
                            "part": norm_id(sub.get("id")),
                            "gap": self.units(gap),
                            "box": self.ubox(dm),
                        }
                    )
        fig = [x for x in strays + parts if x["figure"]]
        return {
            "things": seen,
            "strays": strays,
            "detached_parts": parts,
            "n_strays": len(strays),
            "n_detached_parts": len(parts),
            "n_figure": len(fig),
            "flag_figure": bool(fig),
            "flag_any": bool(strays or parts),
            "flag": bool(fig),
        }

    # ------------------------------------------------------------ b. organic shapes
    def organic(self) -> dict[str, Any]:
        """Polygon-like filled shapes (3 to 5 corners) in living things. Body:
        their share of the visible body. Face: those inside the main mass of the
        head (ears or horns sticking out are not inside the face), as a share of
        that mass; a head whose main mass is itself a polygon counts whole."""
        g = self.g
        rows: list[dict[str, Any]] = []
        for tid, name in self.instances(("character",)):
            if self.living.get(tid) is False:
                continue
            nodes = self.shapes(name)
            if not nodes:
                continue
            nodes, top = self.top_index(nodes)
            vis = np.bincount(top[top >= 0], minlength=len(nodes)).astype(float)
            total = vis.sum()
            if total < 40:
                continue
            filled = [effective(n, g.parent, "fill") != "none" for n in nodes]
            straight = [
                bool(f and v >= 0.004 * total and is_straight(n))
                for n, f, v in zip(nodes, filled, vis)
            ]
            row: dict[str, Any] = {
                "thing": name,
                "shapes": len(nodes),
                "straight": int(sum(straight)),
                "body_share": round(
                    float(sum(v for v, s in zip(vis, straight) if s) / total), 4
                ),
                "face": None,
            }
            head = self.head_node(name)
            if head is not None:
                hid = {id(s) for s in g.shapes_of(head)}
                hn = [(k, n) for k, n in enumerate(nodes) if id(n) in hid]
                if hn:
                    _, main = max(hn, key=lambda kn: int(self.smask(kn[1]).sum()))
                    mass = self.smask(main)
                    area = float(mass.sum())
                    inside = 0.0
                    polys: list[dict[str, Any]] = []
                    for k, n in hn:
                        if not straight[k]:
                            continue
                        m = self.smask(n)
                        if n is main or (m & mass).sum() >= 0.6 * np.maximum(
                            1, m.sum()
                        ):
                            inside += vis[k]
                            polys.append(
                                {
                                    "tag": tag(n),
                                    "corners": corners(n),
                                    "share": round(float(vis[k]) / max(1.0, area), 4),
                                    "box": self.ubox(m),
                                }
                            )
                    if area >= 20:
                        row["face"] = {
                            "id": norm_id(head.get("id")),
                            "shapes": len(hn),
                            "polygons": polys,
                            "share": round(inside / area, 4),
                            "box": self.ubox(self.union([n for _, n in hn])),
                        }
            row["flag_body"] = bool(row["body_share"] >= T["geo_body"])
            row["flag_face"] = bool(
                row["face"] and row["face"]["share"] >= T["geo_face"]
            )
            row["flag"] = row["flag_body"] or row["flag_face"]
            rows.append(row)
        return {
            "things": rows,
            "max_body": max((r["body_share"] for r in rows), default=0.0),
            "max_face": max(
                (r["face"]["share"] for r in rows if r["face"]), default=0.0
            ),
            "faces": sum(1 for r in rows if r["face"]),
            "flag_body": any(r["flag_body"] for r in rows),
            "flag_face": any(r["flag_face"] for r in rows),
            "flag": any(r["flag"] for r in rows),
        }

    # ------------------------------------------------------------ c. depth
    def resting_sets(self) -> tuple[dict[str, list[str]], dict[str, str]]:
        """Instance names resting on one common surface: the things on the same
        support, and the things on the ground."""
        sets: dict[str, list[str]] = {}
        on: dict[str, list[str]] = {}
        for r in self.ck["relations"]:
            if r["type"] == "on":
                on.setdefault(r["b"], []).append(r["a"])
        carried = {r["a"] for r in self.ck["relations"] if r["type"] in CARRY}
        ground = [
            t["id"]
            for t in self.ck["things"]
            if t["kind"] in ("character", "object")
            and t["where"] == "ground"
            and t["id"] not in carried
        ]
        if ground:
            sets["ground"] = ground
        for support, members in on.items():
            # What a character carries on its back does not stand on a plane.
            if self.things[support]["kind"] == "character":
                continue
            sets["on:" + support] = [
                m for m in members if self.things[m]["kind"] in ("character", "object")
            ]
        present = {name: tid for tid, name in self.instances(("character", "object"))}
        out: dict[str, list[str]] = {}
        for key, tids in sets.items():
            names = [n for n, t in present.items() if t in tids]
            if len(names) >= 2:
                out[key] = names
        return out, present

    def depth(self) -> dict[str, Any]:
        g = self.g
        pairs: list[dict[str, Any]] = []
        sizes: list[dict[str, Any]] = []
        bases: dict[str, int] = {}
        sets, present = self.resting_sets()
        contact: set[frozenset[str]] = set()
        for act in self.ck["actions"]:
            for c in act["checks"]:
                if c["check"] in ("contact", "near") and c.get("with"):
                    contact.add(frozenset((act["who"], c["with"])))
        for key, names in sets.items():
            for i in range(len(names)):
                for j in range(i):
                    a, b = names[j], names[i]
                    ta, tb = present[a], present[b]
                    if (ta, tb) in self.carried or (tb, ta) in self.carried:
                        continue
                    ma, mb = g.mask(a), g.mask(b)
                    ba, bb = g.bbox(ma), g.bbox(mb)
                    if ba is None or bb is None:
                        continue
                    ua, ub = self.ubox(ma), self.ubox(mb)
                    assert ua is not None and ub is not None
                    bases[a], bases[b] = ua[3], ub[3]
                    area = np.minimum(ma.sum(), mb.sum())
                    over = ma & mb
                    row: dict[str, Any] = {
                        "set": key,
                        "a": a,
                        "b": b,
                        "base_a": bases[a],
                        "base_b": bases[b],
                        "overlap": round(float(over.sum()) / area, 4),
                        "contact": frozenset((ta, tb)) in contact,
                        "state": "apart",
                    }
                    d = ba[3] - bb[3]
                    if over.sum() >= max(12, T["cover_min"] * area):
                        if abs(d) <= self.px(T["tie"]):
                            row["state"] = (
                                "same_place"
                                if over.sum() >= T["same_overlap"] * area
                                else "ok"
                            )
                        else:
                            far = b if d > 0 else a
                            nodes, top = self.top_index(self.shapes(a) + self.shapes(b))
                            far_ids = {id(s) for s in self.shapes(far)}
                            owner_far = np.isin(
                                top,
                                [k for k, n in enumerate(nodes) if id(n) in far_ids],
                            )
                            cover = owner_far & over
                            row["far"] = far
                            row["far_cover"] = round(float(cover.sum()) / area, 4)
                            row["far_cover_of_overlap"] = round(
                                float(cover.sum()) / np.maximum(1, over.sum()), 3
                            )
                            bad = (
                                cover.sum() >= max(12, T["cover_min"] * area)
                                and cover.sum() >= T["cover_share"] * over.sum()
                            )
                            row["state"] = "far_over_near" if bad else "ok"
                            if bad:
                                row["box"] = self.ubox(ma | mb)
                    if row["state"] == "same_place":
                        row["box"] = self.ubox(ma | mb)
                    pairs.append(row)
                    # Two instances of the same thing: the farther is not taller.
                    if ta == tb and abs(d) > self.px(0.03):
                        ha, hb = ba[3] - ba[1] + 1, bb[3] - bb[1] + 1
                        far_h, near_h = (hb, ha) if d > 0 else (ha, hb)
                        sizes.append(
                            {
                                "a": a,
                                "b": b,
                                "far_over_near_height": round(far_h / near_h, 3),
                                "flag": far_h > T["size_grow"] * near_h,
                            }
                        )
        surfaces = self.surfaces()
        bad_pairs = [p for p in pairs if p["state"] in ("far_over_near", "same_place")]
        return {
            "sets": sets,
            "bases": bases,
            "pairs": pairs,
            "sizes": sizes,
            "surfaces": surfaces,
            "n_pairs": len(pairs),
            "n_bad": len(bad_pairs),
            "n_bad_strict": len([p for p in bad_pairs if not p["contact"]]),
            "flag_order": any(
                p["state"] == "far_over_near" and not p["contact"] for p in pairs
            ),
            "flag_same_place": any(
                p["state"] == "same_place" and not p["contact"] for p in pairs
            ),
            "flag_size": any(s["flag"] for s in sizes),
            "flag_surface": any(s["flag"] for s in surfaces),
            "flag": any(
                p["state"] == "far_over_near" and not p["contact"] for p in pairs
            ),
        }

    def surfaces(self) -> list[dict[str, Any]]:
        """Top face of each support (the largest shape of a thing others rest on):
        not wider at its far (upper) side than at its near (lower) side."""
        g = self.g
        out: list[dict[str, Any]] = []
        supports = {
            r["b"]
            for r in self.ck["relations"]
            if r["type"] == "on" and self.things[r["b"]]["kind"] in ("object", "place")
        }
        for tid in supports:
            for name in self.j.instances(tid):
                nodes = self.shapes(name)
                if not nodes:
                    continue
                best = max(nodes, key=lambda n: int(self.smask(n).sum()))
                m = self.smask(best)
                b = g.bbox(m)
                if (
                    b is None
                    or (b[3] - b[1]) < self.px(0.04)
                    or (b[2] - b[0]) < self.px(0.1)
                ):
                    continue
                top, cut_top = self._widths(m, b, 0.10, 0.25)
                bottom, cut_bottom = self._widths(m, b, 0.75, 0.90)
                if cut_top or cut_bottom or bottom <= 0:
                    continue
                out.append(
                    {
                        "support": name,
                        "far_over_near_width": round(top / bottom, 3),
                        "flag": top > T["widen"] * bottom,
                        "box": self.ubox(m),
                    }
                )
        return out

    def _widths(self, m: Mask, b: Box, lo: float, hi: float) -> tuple[float, bool]:
        """Median row width of a band of a support's top face (shares lo to hi of
        its height), and whether most rows are cut by the canvas."""
        h = b[3] - b[1] + 1
        rows = m[b[1] + int(lo * h) : b[1] + max(int(hi * h), int(lo * h) + 1)]
        ws: list[int] = []
        cut = 0
        for row in rows:
            xs = np.nonzero(row)[0]
            if len(xs):
                ws.append(int(xs[-1] - xs[0] + 1))
                cut += int(xs[0] <= 1 and xs[-1] >= self.g.cw - 2)
        return (float(np.median(ws)) if ws else 0.0), cut > len(rows) / 2

    # ------------------------------------------------------------ d. readability
    def limb_axis(
        self, mask: Mask, centre: npt.NDArray[np.float64]
    ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], float] | None:
        """(root, tip, elongation) of a limb's main axis, or None."""
        ys, xs = np.nonzero(mask)
        if len(xs) < 12:
            return None
        pts = np.stack([xs, ys], 1).astype(np.float64)
        mean = pts.mean(0)
        vals, vecs = np.linalg.eigh(np.cov((pts - mean).T))
        axis = vecs[:, 1]
        proj = (pts - mean) @ axis
        p0, p1 = mean + proj.min() * axis, mean + proj.max() * axis
        if np.hypot(*(p0 - centre)) > np.hypot(*(p1 - centre)):
            p0, p1 = p1, p0
        return p0, p1, float(vals[1] / max(vals[0], 1e-6))

    def _is_limb(self, node_id: str | None) -> bool:
        tokens = norm_id(node_id).split("-")
        return any(any(tok.startswith(w) for w in LIMBS) for tok in tokens)

    def readability(self) -> dict[str, Any]:
        g = self.g
        chars = self.instances(("character",))
        # Overlap between distinct characters.
        overlaps: list[dict[str, Any]] = []
        pending: list[tuple[int, str, str, float]] = []
        for i in range(len(chars)):
            for j in range(i):
                (ta, a), (tb, b) = chars[j], chars[i]
                if (ta, tb) in self.carried or (tb, ta) in self.carried:
                    continue
                ma, mb = g.mask(a), g.mask(b)
                share = float((ma & mb).sum()) / np.maximum(
                    1, np.minimum(ma.sum(), mb.sum())
                )
                lab, n = ndi.label(ma & mb)
                # Separate places where the two cross.
                knots = int((np.bincount(lab[lab > 0])[1:] >= 6).sum()) if n else 0
                overlaps.append(
                    {
                        "a": a,
                        "b": b,
                        "share": round(share, 4),
                        "knots": knots,
                        "flag": share >= T["char_overlap"],
                        "flag_tangle": knots >= T["knots"],
                    }
                )
                pending.append((len(overlaps) - 1, a, b, share))
        # Limbs of one character crossing each other.
        crossings: list[dict[str, Any]] = []
        for _, name in chars:
            limbs: list[ET.Element] = []
            for sub in self.named_descendants(name):
                if not self._is_limb(sub.get("id")):
                    continue
                anc, nested = g.parent.get(id(sub)), False
                while anc is not None and anc is not g.named[name]:
                    if anc.get("id") and self._is_limb(anc.get("id")):
                        nested = True
                    anc = g.parent.get(id(anc))
                if not nested:
                    limbs.append(sub)
            if len(limbs) < 2:
                continue
            limb_ids = {id(s) for limb in limbs for s in g.shapes_of(limb)}
            torso = self.union([s for s in self.shapes(name) if id(s) not in limb_ids])
            whole = g.mask(name)
            cy, cx = ndi.center_of_mass(torso if torso.sum() > 30 else whole)
            centre = np.array([cx, cy])
            axes = []
            for limb in limbs:
                lm = self.union([s for s in g.shapes_of(limb) if tag(s) != "text"])
                axes.append((norm_id(limb.get("id")), lm, self.limb_axis(lm, centre)))
            for i in range(len(axes)):
                for j in range(i):
                    (na, ma, xa), (nb, mb, xb) = axes[j], axes[i]
                    if (
                        xa is None
                        or xb is None
                        or xa[2] < 2.0
                        or xb[2] < 2.0
                        or (ma & mb).sum() < 6
                    ):
                        continue
                    p, r = xa[0], xa[1] - xa[0]
                    q, s = xb[0], xb[1] - xb[0]
                    den = r[0] * s[1] - r[1] * s[0]
                    if abs(den) < 1e-6:
                        continue
                    t = ((q[0] - p[0]) * s[1] - (q[1] - p[1]) * s[0]) / den
                    u = ((q[0] - p[0]) * r[1] - (q[1] - p[1]) * r[0]) / den
                    angle = math.degrees(
                        math.asin(
                            min(1.0, abs(den) / (np.hypot(*r) * np.hypot(*s) + 1e-9))
                        )
                    )
                    if 0.3 <= t <= 1.0 and 0.3 <= u <= 1.0 and angle >= 15:
                        crossings.append(
                            {
                                "thing": name,
                                "a": na,
                                "b": nb,
                                "angle": round(angle),
                                "at": [round(float(t), 2), round(float(u), 2)],
                            }
                        )
        # Value contrast of each subject with what surrounds it.
        rgb = np.asarray(
            g.render(padded=False, size=g.frame_px, background="#ffffff", mode="RGB"),
            np.float32,
        )
        lum = rgb @ np.array([0.299, 0.587, 0.114], np.float32)
        for k, a, b, share in pending:
            # Two overlapping characters of nearly the same colour merge in one mass.
            va, vb = g.in_frame(g.visible(a)), g.in_frame(g.visible(b))
            dist = (
                float(np.linalg.norm(rgb[va].mean(0) - rgb[vb].mean(0)))
                if va.sum() > 10 and vb.sum() > 10
                else None
            )
            overlaps[k]["colour_apart"] = round(dist, 1) if dist is not None else None
            overlaps[k]["flag_merge"] = bool(
                dist is not None and share >= T["merge_share"] and dist < T["merge_rgb"]
            )
        contrasts: list[dict[str, Any]] = []
        for tid, name in self.instances(("character", "object")):
            vis = g.in_frame(g.visible(name))
            if vis.sum() < 0.003 * self.full:
                continue
            ring = ndi.binary_dilation(vis, iterations=self.px(0.03)) & ~g.in_frame(
                g.mask(name)
            )
            if ring.sum() < 20:
                continue
            dl = abs(float(lum[vis].mean()) - float(lum[ring].mean()))
            dc = float(np.linalg.norm(rgb[vis].mean(0) - rgb[ring].mean(0)))
            # Lost edges: along the outline, local mean inside against outside.
            w = 2 * self.px(0.012) + 1
            inside_w = ndi.uniform_filter(vis.astype(np.float32), w) + 1e-6
            outside_w = ndi.uniform_filter((~vis).astype(np.float32), w) + 1e-6
            edge = vis & ~ndi.binary_erosion(vis)
            edge[0, :] = edge[-1, :] = False
            edge[:, 0] = edge[:, -1] = False
            lost = None
            if edge.sum() >= 20:
                diff = np.zeros(vis.shape, np.float32)
                for ch in range(3):
                    a_in = ndi.uniform_filter(rgb[..., ch] * vis, w) / inside_w
                    a_out = ndi.uniform_filter(rgb[..., ch] * (~vis), w) / outside_w
                    diff += (a_in - a_out) ** 2
                l_in = ndi.uniform_filter(lum * vis, w) / inside_w
                l_out = ndi.uniform_filter(lum * (~vis), w) / outside_w
                weak = (np.abs(l_in - l_out) < T["lost_l"]) & (
                    np.sqrt(diff) < T["lost_rgb"]
                )
                lost = round(float(weak[edge].mean()), 3)
            contrasts.append(
                {
                    "thing": name,
                    "kind": self.things[tid]["kind"],
                    "value": round(dl, 1),
                    "colour": round(dc, 1),
                    "lost_edge": lost,
                    "flag": bool(
                        lost is not None
                        and lost >= T["lost_share"]
                        and self.things[tid]["kind"] == "character"
                    ),
                }
            )
        # Clutter.
        nodes = [s for s in g.shapes if tag(s) != "text"]
        tiny = micro = faint = 0
        for shape in nodes:
            box = g.bbox(self.smask(shape))
            if box is None:
                faint += 1
                continue
            side = max(box[2] - box[0] + 1, box[3] - box[1] + 1)
            micro += side < self.px(T["micro"])
            tiny += side < self.px(T["tiny"])
        _, whole_top = self.top_index(nodes)
        top = whole_top[g.oy : g.oy + g.fh, g.ox : g.ox + g.fw]
        fragments = 0
        limit = T["fragment"] * self.full
        for k in np.unique(top[top >= 0]):
            lab, n_found = ndi.label(np.asarray(top == k))
            if n_found:
                found = np.bincount(lab[lab > 0])[1:]
                fragments += int(((found < limit) & (found >= 3)).sum())
        return {
            "char_overlaps": overlaps,
            "max_char_overlap": max((o["share"] for o in overlaps), default=0.0),
            "crossings": crossings,
            "n_crossings": len(crossings),
            "contrasts": contrasts,
            "min_contrast": min((c["value"] for c in contrasts), default=None),
            "n_low_contrast": sum(c["flag"] for c in contrasts),
            "shapes": len(nodes),
            "tiny": int(tiny),
            "micro": int(micro),
            "faint": int(faint),
            "fragments": fragments,
            "max_lost_edge": max(
                (c["lost_edge"] or 0 for c in contrasts if c["kind"] == "character"),
                default=0.0,
            ),
            "max_knots": max((o["knots"] for o in overlaps), default=0),
            "flag_overlap": any(o["flag"] for o in overlaps),
            "flag_tangle": any(o["flag_tangle"] for o in overlaps),
            "flag_merge": any(o.get("flag_merge") for o in overlaps),
            "flag_crossing": bool(crossings),
            "flag_contrast": any(c["flag"] for c in contrasts),
        }

    def run(self) -> dict[str, Any]:
        return {
            "attachment": self.attachment(),
            "organic": self.organic(),
            "depth": self.depth(),
            "readability": self.readability(),
        }


def code_flags(result: dict[str, Any]) -> dict[str, bool]:
    """The four reliable code checks with their scoring rules: a detached part of
    a figure, a polygon in a face, a far thing over a near one, an unreadable
    tangle or crossing between characters or limbs."""
    a, b, d, r = (
        result["attachment"],
        result["organic"],
        result["depth"],
        result["readability"],
    )
    pairs = [o for o in r["char_overlaps"] if o["share"] < 0.8]
    unreadable = (
        any(o["flag_tangle"] or o.get("flag_merge") for o in pairs)
        or r["flag_crossing"]
    )
    return {
        "a": bool(a["flag"] and a["things"] > 0),
        "b": bool(b["flag_face"] and b["faces"] > 0),
        "c": bool(d["flag"] and d["n_pairs"] > 0),
        "d": bool(unreadable and r.get("_chars", 1) > 0),
    }
