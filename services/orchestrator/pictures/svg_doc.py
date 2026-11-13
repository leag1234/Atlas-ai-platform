"""An SVG document as the kits see it: ids, ancestors, transforms, renders.

Shared by the face, body and animal kits, which add elements to the painter's
own document; every render goes through render.py.
"""

from __future__ import annotations

import copy
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence

import numpy as np
import numpy.typing as npt

from services.orchestrator.pictures import render

NS = "http://www.w3.org/2000/svg"
Q = "{%s}" % NS
ET.register_namespace("", NS)
SHAPES = frozenset(
    {"path", "circle", "ellipse", "rect", "polygon", "polyline", "line", "use", "text"}
)
CONTAINERS = frozenset(
    {
        "defs",
        "clipPath",
        "mask",
        "pattern",
        "symbol",
        "marker",
        "filter",
        "linearGradient",
        "radialGradient",
    }
)
HEAD_RE = re.compile(r"^(.+?)-head(?:-(\d+))?$")
_NUMBER = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")

Matrix = npt.NDArray[np.float64]
Rgb = Sequence[float]


def tag(node: ET.Element) -> str:
    return node.tag.split("}")[-1] if isinstance(node.tag, str) else ""


# ---------------------------------------------------------------- transforms
def parse_transform(text: str | None) -> Matrix:
    """The 3x3 matrix of an SVG transform attribute (identity when empty)."""
    m = np.eye(3)
    if not text:
        return m
    for name, args in re.findall(
        r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)", text
    ):
        v = [float(x) for x in _NUMBER.findall(args)]
        t = np.eye(3)
        if name == "matrix" and len(v) == 6:
            t = np.array([[v[0], v[2], v[4]], [v[1], v[3], v[5]], [0, 0, 1]])
        elif name == "translate":
            t[0, 2] = v[0] if v else 0
            t[1, 2] = v[1] if len(v) > 1 else 0
        elif name == "scale":
            sx = v[0] if v else 1
            t[0, 0], t[1, 1] = sx, (v[1] if len(v) > 1 else sx)
        elif name == "rotate" and v:
            a = math.radians(v[0])
            r = np.array(
                [
                    [math.cos(a), -math.sin(a), 0],
                    [math.sin(a), math.cos(a), 0],
                    [0, 0, 1],
                ]
            )
            if len(v) == 3:
                c1, c2 = np.eye(3), np.eye(3)
                c1[:2, 2] = v[1], v[2]
                c2[:2, 2] = -v[1], -v[2]
                r = c1 @ r @ c2
            t = r
        elif name == "skewX" and v:
            t[0, 1] = math.tan(math.radians(v[0]))
        elif name == "skewY" and v:
            t[1, 0] = math.tan(math.radians(v[0]))
        m = m @ t
    return m


def mstr(m: Matrix) -> str:
    """An SVG matrix() transform for a 3x3 matrix."""
    values = (m[0, 0], m[1, 0], m[0, 1], m[1, 1], m[0, 2], m[1, 2])
    return "matrix(" + " ".join(f"{x:.6g}" for x in values) + ")"


# ---------------------------------------------------------------- colours
def rgb_hex(c: Iterable[float]) -> str:
    return "#" + "".join(f"{int(max(0, min(255, round(v)))):02x}" for v in c)


def mix(a: Rgb, b: Rgb, t: float) -> list[float]:
    return [a[i] * (1 - t) + b[i] * t for i in range(3)]


def lum(c: Rgb) -> float:
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


# ---------------------------------------------------------------- the document
class Doc:
    def __init__(self, svg: str) -> None:
        self.root = ET.fromstring(svg)
        self.parent: dict[int, ET.Element] = {
            id(c): p for p in self.root.iter() for c in p
        }
        view_box = self.root.get("viewBox")
        if view_box:
            self.vb = [float(x) for x in re.split(r"[ ,]+", view_box.strip())[:4]]
        else:
            self.vb = [
                0.0,
                0.0,
                float(re.sub(r"[^\d.]", "", self.root.get("width", "1024")) or 1024),
                float(re.sub(r"[^\d.]", "", self.root.get("height", "1024")) or 1024),
            ]

    def by_id(self, i: str) -> ET.Element | None:
        return next((n for n in self.root.iter() if n.get("id") == i), None)

    def ancestors(self, n: ET.Element) -> list[ET.Element]:
        out: list[ET.Element] = []
        p = self.parent.get(id(n))
        while p is not None:
            out.append(p)
            p = self.parent.get(id(p))
        return out

    def ctm(self, n: ET.Element, include_self: bool = True) -> Matrix:
        """local -> root user space."""
        chain = list(reversed(self.ancestors(n)))[1:] + ([n] if include_self else [])
        m = np.eye(3)
        for a in chain:
            m = m @ parse_transform(a.get("transform"))
        return m

    def shapes_under(
        self, n: ET.Element, exclude: Iterable[ET.Element] = ()
    ) -> list[ET.Element]:
        excluded = {id(e) for e in exclude}
        out: list[ET.Element] = []

        def walk(x: ET.Element) -> None:
            if id(x) in excluded or tag(x) in CONTAINERS:
                return
            if tag(x) in SHAPES:
                out.append(x)
            for c in x:
                walk(c)

        walk(n)
        return out

    def render(
        self,
        keep: Iterable[ET.Element],
        region: Sequence[float],
        px: int,
    ) -> npt.NDArray[np.float64]:
        """RGBA array (floats) of the picture with only the shapes in `keep`, over
        region (x0, y0, x1, y1) in root user units, px pixels wide."""
        keep_ids = {id(k) for k in keep}
        clone = copy.deepcopy(self.root)
        pairs = dict(zip((id(n) for n in self.root.iter()), clone.iter()))
        clone_parent = {id(c): p for p in clone.iter() for c in p}

        def inside_container(n: ET.Element) -> bool:
            p = self.parent.get(id(n))
            while p is not None:
                if tag(p) in CONTAINERS:
                    return True
                p = self.parent.get(id(p))
            return False

        for n in list(self.root.iter()):
            if tag(n) in SHAPES and id(n) not in keep_ids and not inside_container(n):
                twin = pairs[id(n)]
                if id(twin) in clone_parent:
                    clone_parent[id(twin)].remove(twin)
        x0, y0, x1, y1 = region
        w, h = x1 - x0, y1 - y0
        ph = max(8, int(round(px * h / w)))
        clone.set("viewBox", f"{x0} {y0} {w} {h}")
        clone.set("width", str(px))
        clone.set("height", str(ph))
        clone.set("preserveAspectRatio", "none")
        pixels = render.pixels(ET.tostring(clone, encoding="unicode"), px, ph)
        return pixels.astype(float)

    def box(self, keep: Iterable[ET.Element], level: int = 100) -> list[float] | None:
        """Box in root user units of the shapes (512 px render of the whole frame)."""
        x, y, w, h = self.vb
        a = self.render(keep, (x, y, x + w, y + h), 512)[..., 3]
        ys, xs = np.nonzero(a >= level)
        if len(xs) == 0:
            return None
        s = w / 512
        return [
            float(x + xs.min() * s),
            float(y + ys.min() * s),
            float(x + (xs.max() + 1) * s),
            float(y + (ys.max() + 1) * s),
        ]


# ---------------------------------------------------------------- heads and parts
def heads_of(doc: Doc) -> list[tuple[str, ET.Element]]:
    """(NAME, head group) for every <g id="NAME-head"> (or NAME-head-k)."""
    out: list[tuple[str, ET.Element]] = []
    for n in doc.root.iter():
        m = HEAD_RE.match(n.get("id") or "")
        if m and tag(n) == "g":
            out.append((m.group(1), n))
    return out


def find_named(
    doc: Doc, base: str, suffix: str, within: ET.Element | None = None
) -> ET.Element | None:
    """<g id="NAME-suffix"> (or NAME-suffix-1), preferring one inside `within`,
    then inside its parent, then its grand-parent."""
    pattern = re.escape(base) + "-" + suffix + r"(?:-\d+)?"
    candidates = [
        n for n in doc.root.iter() if re.fullmatch(pattern, n.get("id") or "")
    ]
    if within is not None:
        chain = [within] + doc.ancestors(within)[:2]
        for w in chain:
            if w is doc.root:
                break
            inside = [n for n in candidates if w in doc.ancestors(n)]
            if inside:
                return inside[0]
        if len(candidates) > 1:
            return None
    return candidates[0] if candidates else None
