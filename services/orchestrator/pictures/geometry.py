"""What code can measure on a free SVG whose things are in named groups.

The picture is never restyled (it may use gradients, CSS and filters): every
mask is a real render of a subset of its shapes.

- Geometry(svg): parse, expand <use>, list drawable shapes, named nodes and
  units (top-most named groups and unnamed pieces);
- mask(name): unoccluded mask of a named node on a padded canvas (what leaves
  the frame is seen);
- visible(name): the part of it the picture shows (not covered by a later
  opaque unit).

Canvas: the frame is `frame` pixels on its longer side, padded by `pad` on
every side; `unit` is viewBox units per pixel.
"""

from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from collections.abc import Hashable, Iterable

import numpy as np
import numpy.typing as npt
from PIL import Image

from services.orchestrator.pictures import ndimage as ndi
from services.orchestrator.pictures import render

NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", NS)
DRAW = frozenset(
    {
        "path",
        "rect",
        "circle",
        "ellipse",
        "polygon",
        "polyline",
        "line",
        "text",
        "image",
    }
)
NONRENDER = frozenset(
    {
        "defs",
        "clipPath",
        "mask",
        "pattern",
        "symbol",
        "marker",
        "linearGradient",
        "radialGradient",
        "filter",
        "style",
        "metadata",
        "title",
        "desc",
    }
)
XLINK = "{http://www.w3.org/1999/xlink}href"

Mask = npt.NDArray[np.bool_]
Box = tuple[int, int, int, int]


def tag(node: ET.Element) -> str:
    return node.tag.split("}")[-1] if isinstance(node.tag, str) else ""


def hidden(node: ET.Element) -> bool:
    """A node the renderer does not show (display none): a painter's guide."""
    return node.get("display") == "none" or "display:none" in (
        node.get("style") or ""
    ).replace(" ", "")


def norm_id(text: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")


def _expand_use(root: ET.Element) -> None:
    """<use href="#x"> becomes a group holding a copy of x (ids of the copy
    removed), so the mask of a group sees it."""
    for _ in range(3):
        ids = {n.get("id"): n for n in root.iter() if n.get("id")}
        parents = {id(c): p for p in root.iter() for c in p}
        uses = [n for n in root.iter() if tag(n) == "use"]
        if not uses:
            return
        for use in uses:
            ref = (use.get("href") or use.get(XLINK) or "").lstrip("#")
            target, parent = ids.get(ref), parents.get(id(use))
            if parent is None:
                continue
            group = ET.Element(f"{{{NS}}}g")
            for key, value in use.attrib.items():
                if key not in ("href", XLINK, "x", "y", "width", "height", "transform"):
                    group.set(key, value)
            shift = f"translate({use.get('x') or 0},{use.get('y') or 0})"
            group.set("transform", ((use.get("transform") or "") + " " + shift).strip())
            if target is not None:
                clone = copy.deepcopy(target)
                if tag(clone) == "symbol":
                    clone.tag = f"{{{NS}}}g"
                    for key in ("viewBox", "preserveAspectRatio"):
                        clone.attrib.pop(key, None)
                for n in clone.iter():
                    n.attrib.pop("id", None)
                group.append(clone)
            group.tail = use.tail
            index = list(parent).index(use)
            parent.remove(use)
            parent.insert(index, group)


def thing_instances(named: Iterable[str], tid: str) -> list[str]:
    """Named groups standing for a checklist thing: its numbered instances
    (tid-1, tid-2 ...) when there are some, else tid itself or its plural."""
    names = list(named)
    numbered = sorted(
        (n for n in names if re.fullmatch(re.escape(tid) + r"s?-\d+", n)),
        key=lambda n: int(n.rsplit("-", 1)[1]),
    )
    if numbered:
        return numbered
    return [n for n in (tid, tid + "s", tid + "es") if n in names][:1]


@dataclass
class Unit:
    """A top-most named group or shape, or a run of unnamed shapes."""

    id: str | None
    nodes: list[ET.Element]
    first: int


class Geometry:
    def __init__(self, svg: str, frame: int = 400, pad: float = 0.2) -> None:
        self.svg = svg
        self.root = ET.fromstring(svg)
        _expand_use(self.root)
        self.frame_px, self.pad = frame, pad
        view_box = self.root.get("viewBox")
        vb = (
            [float(v) for v in re.split(r"[ ,]+", view_box.strip()) if v]
            if view_box
            else []
        )
        if len(vb) != 4 or vb[2] <= 0 or vb[3] <= 0:
            vb = [0.0, 0.0, self._size("width"), self._size("height")]
        self.vb = vb
        scale = frame / max(vb[2], vb[3])
        self.fw, self.fh = int(round(vb[2] * scale)), int(round(vb[3] * scale))
        self.ox, self.oy = int(round(self.fw * pad)), int(round(self.fh * pad))
        self.cw, self.ch = self.fw + 2 * self.ox, self.fh + 2 * self.oy
        self.unit = vb[2] / self.fw
        self.parent = {id(c): p for p in self.root.iter() for c in p}
        self.shapes: list[ET.Element] = []  # drawable leaves in painter's order
        self.named: dict[str, ET.Element] = {}  # normalised id -> node, outside defs
        self._walk(self.root)
        self.order = {id(n): k for k, n in enumerate(self.shapes)}
        # Masks and other measurements, keyed by their callers.
        self.cache: dict[Hashable, Mask] = {}
        self.units: list[Unit] = []
        self._units(self.root)
        self._unit_masks: list[Mask] | None = None
        self.top: npt.NDArray[np.int32] = np.full((self.ch, self.cw), -1, np.int32)

    def _size(self, name: str) -> float:
        found = re.match(r"[\d.]+", self.root.get(name) or "")
        try:
            return float(found.group(0)) if found else 1024.0
        except ValueError:
            return 1024.0

    # ------------------------------------------------------------ structure
    def _walk(self, node: ET.Element) -> None:
        for child in node:
            t = tag(child)
            if t in NONRENDER or not t or hidden(child):
                continue
            if child.get("id") and (t == "g" or t in DRAW):
                self.named.setdefault(norm_id(child.get("id")), child)
            if t in DRAW:
                self.shapes.append(child)
            else:
                self._walk(child)

    def shapes_of(self, node: ET.Element) -> list[ET.Element]:
        if tag(node) in DRAW:
            return [node]
        out: list[ET.Element] = []

        def walk(n: ET.Element) -> None:
            for child in n:
                t = tag(child)
                if t in NONRENDER or not t:
                    continue
                if t in DRAW:
                    out.append(child)
                else:
                    walk(child)

        walk(node)
        return out

    def _has_named(self, node: ET.Element) -> bool:
        return any(
            n.get("id") and (tag(n) == "g" or tag(n) in DRAW) and not hidden(n)
            for n in node.iter()
            if n is not node
        )

    def _units(self, node: ET.Element) -> None:
        for child in node:
            t = tag(child)
            if t in NONRENDER or not t or hidden(child):
                continue
            if child.get("id") and (t == "g" or t in DRAW):
                nodes = self.shapes_of(child)
                if nodes:
                    self.units.append(
                        Unit(norm_id(child.get("id")), nodes, self.order[id(nodes[0])])
                    )
            elif t == "g" and self._has_named(child):
                self._units(child)
            else:
                nodes = self.shapes_of(child)
                if nodes:
                    self.units.append(Unit(None, nodes, self.order[id(nodes[0])]))
        if node is self.root and len(self.units) > 220:
            # A picture of hundreds of loose shapes: merge unnamed runs.
            merged: list[Unit] = []
            for unit in self.units:
                if (
                    unit.id is None
                    and merged
                    and merged[-1].id is None
                    and len(merged[-1].nodes) < 12
                ):
                    merged[-1].nodes += unit.nodes
                else:
                    merged.append(unit)
            self.units = merged

    def lineage(self, node: ET.Element | None) -> list[str]:
        """Normalised ids of the node and of its named ancestors."""
        out: list[str] = []
        while node is not None:
            if node.get("id"):
                out.append(norm_id(node.get("id")))
            node = self.parent.get(id(node))
        return out

    def text_nodes(self) -> list[ET.Element]:
        return [n for n in self.shapes if tag(n) == "text"]

    @staticmethod
    def text_of(node: ET.Element) -> str:
        return re.sub(r"\s+", " ", "".join(node.itertext())).strip()

    # ------------------------------------------------------------ renders
    def render(
        self,
        keep: list[ET.Element] | None = None,
        drop: list[ET.Element] | None = None,
        padded: bool = True,
        size: int | None = None,
        background: str | None = None,
        filters: bool = True,
        mode: str = "RGBA",
    ) -> Image.Image:
        """Render with only the shapes in `keep` (or without those in `drop`)."""
        clone = copy.deepcopy(self.root)
        if keep is not None or drop is not None:
            keep_ids = {id(n) for n in keep} if keep is not None else None
            drop_ids = {id(n) for n in drop} if drop is not None else set()
            pairs = dict(zip((id(n) for n in self.root.iter()), clone.iter()))
            clone_parent = {id(c): p for p in clone.iter() for c in p}
            for shape in self.shapes:
                gone = (
                    (id(shape) not in keep_ids)
                    if keep_ids is not None
                    else (id(shape) in drop_ids)
                )
                if gone:
                    twin = pairs[id(shape)]
                    clone_parent[id(twin)].remove(twin)
        if not filters:
            for node in clone.iter():
                node.attrib.pop("filter", None)
                style = node.get("style")
                if style and "filter" in style:
                    node.set("style", re.sub(r"filter\s*:[^;]*;?", "", style))
        x, y, w, h = self.vb
        if padded:
            clone.set(
                "viewBox",
                f"{x - self.pad * w} {y - self.pad * h} "
                f"{w * (1 + 2 * self.pad)} {h * (1 + 2 * self.pad)}",
            )
            width, height = self.cw, self.ch
        else:
            clone.set("viewBox", f"{x} {y} {w} {h}")
            scale = (size or self.frame_px) / max(self.fw, self.fh)
            width, height = int(round(self.fw * scale)), int(round(self.fh * scale))
        if padded and size:
            scale = size / max(self.fw, self.fh)
            width, height = int(round(self.cw * scale)), int(round(self.ch * scale))
        clone.attrib.pop("width", None)
        clone.attrib.pop("height", None)
        clone.set("width", str(width))
        clone.set("height", str(height))
        return render.image(
            ET.tostring(clone, encoding="unicode"),
            width,
            height,
            background=background,
            mode=mode,
        )

    def picture(
        self,
        size: int = 1024,
        background: str = "#ffffff",
        drop: list[ET.Element] | None = None,
        keep: list[ET.Element] | None = None,
    ) -> Image.Image:
        return self.render(
            keep=keep,
            drop=drop,
            padded=False,
            size=size,
            background=background,
            mode="RGB",
        )

    def alpha(self, nodes: list[ET.Element], level: int = 128) -> Mask:
        if not nodes:
            return np.zeros((self.ch, self.cw), bool)
        a = np.asarray(self.render(keep=nodes, filters=False))[..., 3]
        return np.asarray(a >= level)

    def mask(self, name: str, level: int = 128) -> Mask:
        """Unoccluded mask of a named node (padded canvas)."""
        key = ("m", name, level)
        if key not in self.cache:
            node = self.named.get(name)
            self.cache[key] = (
                self.alpha(self.shapes_of(node), level)
                if node is not None
                else np.zeros((self.ch, self.cw), bool)
            )
        return self.cache[key]

    def unit_masks(self) -> list[Mask]:
        if self._unit_masks is None:
            masks: list[Mask] = []
            for unit in self.units:
                level = 60 if all(tag(n) == "text" for n in unit.nodes) else 128
                m = self.alpha(unit.nodes, level)
                masks.append(m)
                if unit.id:
                    self.cache[("m", unit.id, level)] = m
                    self.cache.setdefault(("m", unit.id, 128), m)
            self._unit_masks = masks
            top = np.full((self.ch, self.cw), -1, np.int32)  # unit shown per pixel
            for k, m in enumerate(masks):
                top[m] = k
            self.top = top
        return self._unit_masks

    def unit_of(self, name: str) -> int | None:
        """Index of the unit holding the named node (itself, or its top-most
        named ancestor)."""
        node = self.named.get(name)
        if node is None:
            return None
        line = self.lineage(node)
        for k, unit in enumerate(self.units):
            if unit.id and unit.id in line:
                return k
        return None

    def visible(self, name: str) -> Mask:
        """What the picture shows of a named node: its mask minus what later
        opaque units cover."""
        self.unit_masks()
        k = self.unit_of(name)
        m = self.mask(name)
        if k is None:
            return m
        return np.asarray(m & (self.top == k))

    # ------------------------------------------------------------ helpers on masks
    def in_frame(self, mask: Mask) -> Mask:
        return mask[self.oy : self.oy + self.fh, self.ox : self.ox + self.fw]

    def frame_only(self, mask: Mask) -> Mask:
        out = np.zeros_like(mask)
        out[self.oy : self.oy + self.fh, self.ox : self.ox + self.fw] = mask[
            self.oy : self.oy + self.fh, self.ox : self.ox + self.fw
        ]
        return out

    @staticmethod
    def bbox(mask: Mask) -> Box | None:
        ys, xs = np.nonzero(mask)
        if not len(xs):
            return None
        return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())

    def ubox(self, mask: Mask) -> Box | None:
        """Bounding box in viewBox units (x0, y0, x1, y1), or None."""
        b = self.bbox(mask)
        if b is None:
            return None
        return (
            round((b[0] - self.ox) * self.unit + self.vb[0]),
            round((b[1] - self.oy) * self.unit + self.vb[1]),
            round((b[2] + 1 - self.ox) * self.unit + self.vb[0]),
            round((b[3] + 1 - self.oy) * self.unit + self.vb[1]),
        )

    def px(self, share: float) -> int:
        """Pixels for a share of the frame's longer side."""
        return max(1, int(round(share * max(self.fw, self.fh))))

    def units_of(self, pixels: float) -> int:
        return int(round(pixels * self.unit))

    def touch(self, a: Mask, b: Mask, tol: float = 0.02) -> bool:
        if not a.any() or not b.any():
            return False
        return bool((ndi.binary_dilation(a, iterations=self.px(tol)) & b).any())

    def gap(self, a: Mask, b: Mask) -> float | None:
        """Smallest distance between two masks in pixels (0 when they overlap);
        None if one is empty."""
        if not a.any() or not b.any():
            return None
        return float(ndi.distance_transform_edt(~b)[a].min())

    def base_band(self, mask: Mask, share: float = 0.12) -> Mask:
        b = self.bbox(mask)
        if b is None:
            return mask
        out = np.zeros_like(mask)
        cut = b[3] - max(2, int(share * (b[3] - b[1] + 1)))
        out[cut:] = mask[cut:]
        return out
