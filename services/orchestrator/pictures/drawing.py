"""Drawing helpers shared by the body kit and the animal kit.

Paths in root user units (smooth closed splines, rounded polygons, capsules,
ellipses), colours, one-outline layers in the painter's own line style, the
painter's anchors and the declared relations to things.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, overload

import numpy as np
import numpy.typing as npt

from services.orchestrator.pictures.svg_doc import SHAPES, Doc, find_named, mstr, tag

NS = "http://www.w3.org/2000/svg"
Vec = npt.NDArray[np.float64]
Anchors = dict[str, dict[str, Any]]


def named_part(
    doc: Doc, name: str, suffix: str, within: ET.Element | None
) -> ET.Element | None:
    """<g id="NAME-suffix">, else the nearest NAME-suffix(-k) found around
    `within` (an empty group counts as missing, as in the spike)."""
    node = doc.by_id(f"{name}-{suffix}")
    if node is None or len(node) == 0:
        node = find_named(doc, name, suffix, within)
    return node


FACINGS = (
    "front",
    "three-quarter-left",
    "three-quarter-right",
    "profile-left",
    "profile-right",
    "back",
)


def el(name: str, **attrs: object) -> ET.Element:
    return ET.Element(
        f"{{{NS}}}{name}", {k.replace("_", "-"): str(v) for k, v in attrs.items()}
    )


def V(p: Any) -> Vec:
    return np.array([float(p[0]), float(p[1])])


def unit(v: Vec, default: Sequence[float] = (0.0, 1.0)) -> Vec:
    n = float(np.hypot(*v))
    return v / n if n > 1e-6 else np.array(default, float)


def rot(v: Vec, a: float) -> Vec:
    c, s = math.cos(a), math.sin(a)
    return np.array([c * v[0] - s * v[1], s * v[0] + c * v[1]])


def cross(a: Vec, b: Vec) -> float:
    return float(a[0] * b[1] - a[1] * b[0])


@overload
def hexcol(c: object, default: str) -> str: ...
@overload
def hexcol(c: object, default: None) -> str | None: ...
def hexcol(c: object, default: str | None) -> str | None:
    """A #rrggbb colour (lower case), or the default."""
    c = (c or "").strip() if isinstance(c, str) else ""
    if re.fullmatch(r"#[0-9a-fA-F]{6}", c):
        return c.lower()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", c):
        return "#" + "".join(ch * 2 for ch in c[1:]).lower()
    return default


def shade(c: str, k: float) -> str:
    """k < 0 darker, k > 0 lighter."""
    r, g, b = int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
    if k < 0:
        r, g, b = (int(v * (1 + k)) for v in (r, g, b))
    else:
        r, g, b = (int(v + (255 - v) * k) for v in (r, g, b))
    return "#%02x%02x%02x" % (
        max(0, min(255, r)),
        max(0, min(255, g)),
        max(0, min(255, b)),
    )


def fmt(p: Any) -> str:
    return f"{p[0]:.1f},{p[1]:.1f}"


# ---------------------------------------------------------------- shapes (root user units)
def capsule(a: Vec, b: Vec, ra: float, rb: float, n: int = 9) -> str:
    d = b - a
    if np.hypot(*d) < 1e-3:
        return circle_d(a, max(ra, rb))
    delta = math.atan2(d[1], d[0])
    pts = []
    for i in range(n + 1):
        t = delta + math.pi / 2 - math.pi * i / n
        pts.append(b + rb * np.array([math.cos(t), math.sin(t)]))
    for i in range(n + 1):
        t = delta - math.pi / 2 - math.pi * i / n
        pts.append(a + ra * np.array([math.cos(t), math.sin(t)]))
    return "M" + " L".join(fmt(p) for p in pts) + " Z"


def circle_d(c: Vec, r: float) -> str:
    return f"M{c[0] - r:.1f},{c[1]:.1f} a{r:.1f},{r:.1f} 0 1,0 {2 * r:.1f},0 a{r:.1f},{r:.1f} 0 1,0 {-2 * r:.1f},0 Z"


def ellipse_d(c: Vec, rx: float, ry: float, ang: float) -> str:
    pts = [
        c + rot(np.array([rx * math.cos(t), ry * math.sin(t)]), ang)
        for t in np.linspace(0, 2 * math.pi, 24, endpoint=False)
    ]
    return smooth(pts)


def smooth(pts: Iterable[Any], closed: bool = True, tension: float = 1.0) -> str:
    """Closed Catmull-Rom spline through the points, as cubic Beziers."""
    p = [np.asarray(x, float) for x in pts]
    n = len(p)
    if n < 3:
        return "M" + " L".join(fmt(x) for x in p) + (" Z" if closed else "")
    out = "M" + fmt(p[0])
    rng = range(n) if closed else range(n - 1)
    for i in rng:
        p0, p1, p2, p3 = p[(i - 1) % n], p[i], p[(i + 1) % n], p[(i + 2) % n]
        if not closed:
            p0 = p[max(i - 1, 0)]
            p3 = p[min(i + 2, n - 1)]
        c1 = p1 + (p2 - p0) * tension / 6
        c2 = p2 - (p3 - p1) * tension / 6
        out += f" C{fmt(c1)} {fmt(c2)} {fmt(p2)}"
    return out + (" Z" if closed else "")


def rounded(pts: Iterable[Any], f: float = 0.25) -> str:
    """Closed polygon with rounded corners (straighter sides than a spline through few points)."""
    v = [np.asarray(x, float) for x in pts]
    n = len(v)
    if n < 3:
        return smooth(v)
    a = [v[i] + (v[i - 1] - v[i]) * f for i in range(n)]
    b = [v[i] + (v[(i + 1) % n] - v[i]) * f for i in range(n)]
    out = "M" + fmt(b[0])
    for i in range(1, n + 1):
        j = i % n
        out += f" L{fmt(a[j])} Q{fmt(v[j])} {fmt(b[j])}"
    return out + " Z"


def hull(points: Iterable[Any]) -> list[Vec]:
    pts = sorted({(round(float(p[0]), 2), round(float(p[1]), 2)) for p in points})
    if len(pts) < 3:
        return [np.array(p) for p in pts]

    def half(seq: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
        h: list[tuple[float, float]] = []
        for q in seq:
            while (
                len(h) >= 2
                and (h[-1][0] - h[-2][0]) * (q[1] - h[-2][1])
                - (h[-1][1] - h[-2][1]) * (q[0] - h[-2][0])
                <= 0
            ):
                h.pop()
            h.append(q)
        return h

    lo, up = half(pts), half(reversed(pts))
    return [np.array(p) for p in lo[:-1] + up[:-1]]


# ---------------------------------------------------------------- drawing
class Style:
    def __init__(self, doc: Doc) -> None:
        cols: Counter[str] = Counter()
        widths: list[float] = []
        n = 0
        for x in doc.root.iter():
            if tag(x) in SHAPES and tag(x) != "text":
                n += 1
                st = x.get("stroke") or ""
                m = re.search(r"stroke\s*:\s*([^;]+)", x.get("style") or "")
                if m:
                    st = m.group(1).strip()
                colour = hexcol(st, None)
                if st and st != "none" and colour:
                    cols[colour] += 1
                    w = x.get("stroke-width")
                    try:
                        widths.append(float(re.sub(r"[^\d.]", "", w)) if w else 1.0)
                    except ValueError:
                        pass
        self.share = sum(cols.values()) / max(1, n)
        dark = [
            (c, k)
            for c, k in cols.most_common()
            if sum(int(c[i : i + 2], 16) for i in (1, 3, 5)) < 3 * 110
        ]
        self.line = dark[0][0] if dark and self.share >= 0.2 else None
        self.width = float(np.median(widths)) if widths else None
        self.rec = {
            "stroked_share": round(self.share, 2),
            "line": self.line,
            "width": self.width,
        }

    def lw(self, h: float) -> float:
        w = self.width if self.width else 0.03 * h
        return max(0.8, min(w, 0.06 * h, 4.0))

    def ink(self, fill: str) -> str:
        return self.line or shade(fill, -0.55)


class Layer:
    """Shapes of one silhouette: drawn once fat in the line colour (underlay: one outline around the union), then filled."""

    def __init__(self, gid: str) -> None:
        self.gid = gid
        self.items: list[tuple[str, str, bool | tuple[str, float]]] = []

    def add(self, d: str, fill: str, outline: bool = True) -> None:
        self.items.append((d, fill, outline))

    def line(self, d: str, color: str, k: float = 0.7) -> None:
        """round 21: a stroke-only fold line drawn with the fills, in order."""
        self.items.append((d, color, ("line", k)))

    def element(self, style: Style, h: float, darker: float = 0.0) -> ET.Element:
        g = el("g", id=self.gid)
        lw = style.lw(h)
        for d, fill, outline in self.items:
            if outline is True:
                fill2 = shade(fill, -darker) if darker else fill
                ink = style.ink(fill2)
                g.append(
                    el(
                        "path",
                        d=d,
                        fill=ink,
                        stroke=ink,
                        stroke_width=f"{2 * lw:.2f}",
                        stroke_linejoin="round",
                    )
                )
        for d, fill, outline in self.items:
            if isinstance(outline, tuple):
                c = shade(fill, -darker) if darker else fill
                g.append(
                    el(
                        "path",
                        d=d,
                        fill="none",
                        stroke=c,
                        stroke_width=f"{max(0.6, lw * outline[1]):.2f}",
                        stroke_linecap="round",
                    )
                )
            else:
                g.append(el("path", d=d, fill=shade(fill, -darker) if darker else fill))
        return g


# ---------------------------------------------------------------- composition
def anchor_points(doc: Doc, ids: set[str]) -> Anchors:
    out = {}
    for n in doc.root.iter():
        i = n.get("id") or ""
        if not i.startswith("anchor-"):
            continue
        t = tag(n)
        try:
            if t == "circle" or t == "ellipse":
                x, y = float(n.get("cx", 0)), float(n.get("cy", 0))
            elif t == "rect":
                x = float(n.get("x", 0)) + float(n.get("width", 0)) / 2
                y = float(n.get("y", 0)) + float(n.get("height", 0)) / 2
            else:
                continue
        except ValueError:
            continue
        m = doc.ctm(n)
        q = m @ np.array([x, y, 1.0])
        owner = None
        anc = doc.ancestors(n)
        for a in anc:
            if a is not doc.root and (a.get("id") or "") in ids:
                owner = a
        if owner is None:
            owner = next(
                (
                    a
                    for a in anc
                    if a is not doc.root
                    and a.get("id")
                    and not str(a.get("id")).startswith("anchor")
                ),
                None,
            )
        out[i] = {"p": np.array([q[0], q[1]]), "owner": owner}
    return out


def find_anchor(anchors: Mapping[str, Any], name: str) -> str | None:
    if name in anchors:
        return name
    if not name.startswith("anchor-") and "anchor-" + name in anchors:
        return "anchor-" + name
    return None


def wrap_inverse(doc: Doc, parent: ET.Element, child: ET.Element) -> ET.Element:
    """Wrap root-coordinate drawing so that it lands right under `parent` (inverse of the parent's transform chain)."""
    m = doc.ctm(parent) if parent is not doc.root else np.eye(3)
    if np.allclose(m, np.eye(3)):
        return child
    g = el("g", transform=mstr(np.linalg.inv(m)))
    g.append(child)
    return g


def lca_children(
    doc: Doc, a: ET.Element, b: ET.Element
) -> tuple[ET.Element, ET.Element | None, ET.Element | None]:
    """(common ancestor, its child holding a, its child holding b)."""
    ca = [a] + doc.ancestors(a)
    cb = [b] + doc.ancestors(b)
    sb = {id(x) for x in cb}
    common = next(x for x in ca if id(x) in sb)
    ka = ca[ca.index(common) - 1] if ca.index(common) > 0 else None
    ib = [id(x) for x in cb].index(id(common))
    kb = cb[ib - 1] if ib > 0 else None
    return common, ka, kb


def head_box(
    doc: Doc, head: ET.Element, exclude: Iterable[ET.Element]
) -> list[float] | None:
    shapes = doc.shapes_under(head, exclude=exclude)
    return doc.box(shapes, level=60) if shapes else None


# ---------------------------------------------------------------- round 22: declared relations, lying poses, two hands on one long thing, hair off the face
RELS = {
    "in": "in",
    "inside": "in",
    "into": "in",
    "on": "on",
    "onto": "on",
    "on-top-of": "on",
    "sitting-on": "on",
    "standing-on": "on",
    "behind": "behind",
    "in-front-of": "in-front-of",
    "in-front": "in-front-of",
    "front": "in-front-of",
    "before": "in-front-of",
    "holding": "holding",
    "hold": "holding",
    "holds": "holding",
    "carrying": "holding",
    "beside": "beside",
    "next-to": "beside",
    "near": "beside",
    "by": "beside",
    "lying-on": "lying-on",
    "lying-in": "lying-in",
    "lies-on": "lying-on",
    "lies-in": "lying-in",
    "lying": "lying-on",
    "reclining-on": "lying-on",
    "reclining-in": "lying-in",
    "under": "lying-in",
}


def parse_relations(
    raw: dict[str, Any],
) -> tuple[list[dict[str, str]] | None, list[str]]:
    out: list[dict[str, str]] = []
    problems: list[str] = []
    rels = raw.get("relations")
    if rels is None:
        return None, problems
    if isinstance(rels, dict):
        rels = [{"thing": k, "rel": v} for k, v in rels.items()]
    if not isinstance(rels, list):
        return [], ["relations not a list"]
    for r in rels:
        if not isinstance(r, dict):
            continue
        t = str(r.get("thing") or r.get("id") or "").strip()
        k = (
            str(r.get("rel") or r.get("relation") or "")
            .strip()
            .lower()
            .replace("_", "-")
            .replace(" ", "-")
        )
        k2 = RELS.get(k)
        if not t or k2 is None:
            problems.append(f"relation {t}:{k} dropped")
            continue
        out.append({"thing": t, "rel": k2})
    return out, problems


def find_thing(
    doc: Doc, tid: str, persons_groups: Sequence[ET.Element], g: ET.Element
) -> ET.Element | None:
    """The thing's group: exact id, else numbered instance, else a group whose id starts with it; never a person or the person's own group."""
    cands = []
    n = doc.by_id(tid)
    if n is not None:
        cands.append(n)
    if not cands:
        pat = re.compile(rf"^{re.escape(tid)}-\d+$")
        cands = [
            x for x in doc.root.iter() if tag(x) == "g" and pat.match(x.get("id") or "")
        ]
    if not cands:
        cands = [
            x
            for x in doc.root.iter()
            if tag(x) == "g"
            and (x.get("id") or "").startswith(tid)
            and not (x.get("id") or "").startswith("anchor")
        ][:1]
    for T in cands:
        if T is g or g in doc.ancestors(T) or T in doc.ancestors(g):
            continue
        if any(
            T is q or q in doc.ancestors(T) or T in doc.ancestors(q)
            for q in persons_groups
        ):
            continue
        if tag(T) not in ("g",) and tag(T) not in SHAPES:
            continue
        return T
    return None


def thing_shapes(doc: Doc, T: ET.Element) -> list[ET.Element]:
    if tag(T) != "g":
        return [T]
    return [
        s
        for s in doc.shapes_under(T)
        if tag(s) not in ("text", "use")
        and not (s.get("id") or "").startswith("anchor-")
    ]


def reorder(doc: Doc, g: ET.Element, T: ET.Element, over: bool) -> bool | None:
    """Move the person's branch after (over=True) or before (over=False) the thing's branch in their common parent. -> True if done or
    already right, None if they cannot be reordered (nested)."""
    common, kp, ko = lca_children(doc, g, T)
    if kp is None or ko is None or kp is ko:
        return None
    kids = list(common)
    if over and kids.index(kp) < kids.index(ko):
        common.remove(kp)
        common.insert(list(common).index(ko) + 1, kp)
    elif not over and kids.index(kp) > kids.index(ko):
        common.remove(kp)
        common.insert(list(common).index(ko), kp)
    doc.parent = {id(c): p for p in doc.root.iter() for c in p}
    return True
