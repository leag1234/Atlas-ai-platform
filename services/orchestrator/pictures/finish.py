"""Zero-token finish by code (round 13b): contour and contact shadow.

For each named group standing for a character or an object of the checklist,
the silhouette is taken from the render of the group alone (alpha mask); code
then adds, under the figure,

- a contour: an SVG filter on a wrapper group dilates the opaque alpha and
  floods it with a colour derived from the group's own darkest colours; its
  thickness follows the size of the figure (1.5 to 6 units);
- a soft contact shadow (a blurred dark ellipse) under things resting on a
  surface.

Only opaque figures get a contour: never a surface or a background, nothing
translucent (smoke, light, water, glass), nothing made of thin lines, no
lettering, nothing inside another outlined thing (contour_rule).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import numpy as np

from services.orchestrator.pictures import ndimage as ndi
from services.orchestrator.pictures.geometry import (
    Geometry,
    norm_id,
    tag,
    thing_instances,
)

NS = "http://www.w3.org/2000/svg"
Q = "{%s}" % NS
ET.register_namespace("", NS)
RADIUS = {"thin": 2.0, "medium": 3.5, "thick": 6.0}

Measure = dict[str, Any]


def measures(svg: str, ck: dict[str, Any]) -> list[Measure]:
    """Per candidate group (instances of the checklist things of kind character
    or object): what the contour and shadow rules need."""
    g = Geometry(svg, frame=400)
    rawmap: dict[str, str] = {}
    for i in [n.get("id") for n in ET.fromstring(svg).iter() if n.get("id")]:
        assert i is not None
        rawmap.setdefault(norm_id(i), i)
    full = g.fw * g.fh
    on = {r["a"] for r in ck["relations"] if r["type"] == "on"}
    out: list[Measure] = []
    for t in ck["things"]:
        if t["kind"] not in ("character", "object"):
            continue
        for name in thing_instances(g.named, t["id"]):
            node = g.named.get(name)
            if node is None or tag(node) != "g" or name not in rawmap:
                continue
            shapes = [s for s in g.shapes_of(node) if tag(s) != "text"]
            if not shapes:
                continue
            rgba = np.asarray(g.render(keep=shapes, filters=True)).astype(float)
            a = rgba[..., 3]
            seen = np.asarray(a >= 26)
            solid = np.asarray(a >= 230)
            # Away from the antialiased edge.
            inner = ndi.binary_erosion(seen, iterations=2)
            thin = 1.0 - float(inner.sum()) / max(1, int(seen.sum()))
            mask = g.mask(name)
            area = int(g.in_frame(mask).sum())
            box = g.ubox(mask)
            if not box or area == 0:
                continue
            fb = g.bbox(g.in_frame(mask))
            assert fb is not None
            sides = sum([fb[0] <= 1, fb[1] <= 1, fb[2] >= g.fw - 2, fb[3] >= g.fh - 2])
            lum = 0.299 * rgba[..., 0] + 0.587 * rgba[..., 1] + 0.114 * rgba[..., 2]
            pix = rgba[solid][:, :3]
            if len(pix):
                levels = lum[solid]
                dark = pix[levels <= np.percentile(levels, 8)].mean(axis=0)
            else:
                dark = np.array([40.0, 40.0, 40.0])
            body = float(np.median(lum[solid])) if solid.any() else 0.0
            ys, xs = np.nonzero(mask)
            y1 = ys.max()
            band = ys >= y1 - max(2, int(0.12 * (y1 - ys.min() + 1)))
            bx0, bx1 = xs[band].min(), xs[band].max()
            base = (
                (bx0 - g.ox) * g.unit + g.vb[0],
                (bx1 + 1 - g.ox) * g.unit + g.vb[0],
                (y1 + 1 - g.oy) * g.unit + g.vb[1],
            )
            solid_share = (
                round(float((solid & inner).sum()) / max(1, int(inner.sum())), 3)
                if inner.sum() >= 40
                else 0.0
            )
            ink = 0.55 * (0.299 * dark[0] + 0.587 * dark[1] + 0.114 * dark[2])
            out.append(
                {
                    "id": rawmap[name],
                    "name": name,
                    "thing": t["id"],
                    "kind": t["kind"],
                    "where": t["where"],
                    "on": t["id"] in on,
                    "share": round(area / full, 4),
                    "solid": solid_share,
                    "thin": round(thin, 3),
                    "sides": sides,
                    "box": box,
                    "width_share": round((box[2] - box[0]) / g.vb[2], 3),
                    "dark": [int(v) for v in dark],
                    "body_lum": round(body, 1),
                    "ink_lum": round(float(ink), 1),
                    "base": [round(float(v), 1) for v in base],
                    "bottom_cut": bool(fb[3] >= g.fh - 2),
                    "texts": sum(1 for s in g.shapes_of(node) if tag(s) == "text"),
                    "shapes": len(shapes),
                }
            )
    names = {m["id"] for m in out}
    root = ET.fromstring(svg)
    parent = {id(c): p for p in root.iter() for c in p}
    for m in out:  # nested inside another candidate group, or under a transform
        node_ = next(n for n in root.iter() if n.get("id") == m["id"])
        p = parent.get(id(node_))
        inside: str | None = None
        moved = False
        while p is not None:
            if p.get("id") in names and inside is None:
                inside = p.get("id")
            if p.get("transform"):
                moved = True
            p = parent.get(id(p))
        m["inside"], m["moved_parent"] = inside, moved
    return out


def contour_rule(m: Measure) -> tuple[bool, str]:
    """Which groups get a contour, with the reason."""
    if m["inside"]:
        return False, "inside another outlined thing"
    if m["share"] < 0.004:
        return False, "too small"
    if m["share"] > 0.45 or m["sides"] >= 3:
        return False, "a surface or a background, not a figure"
    if m["body_lum"] - m["ink_lum"] < 30:
        return False, "as dark as its own contour: a contour would only fatten it"
    if m["thin"] > 0.45:
        return False, "made of thin lines: a contour would only thicken them"
    if m["solid"] < 0.85:
        return False, "translucent (smoke, light, water, glass): it must melt in"
    if m["texts"] and m["texts"] >= m["shapes"]:
        return False, "writing"
    return True, "opaque figure"


def shadow_rule(m: Measure) -> tuple[bool, str]:
    """Which groups get a contact shadow, with the reason."""
    if m["inside"]:
        return False, "inside another thing"
    if not (m["where"] == "ground" or m["on"]):
        return False, "does not rest on a surface"
    if m["bottom_cut"]:
        return False, "cut by the bottom of the frame"
    if m["width_share"] > 0.6 or m["share"] > 0.45:
        return False, "a surface, not a thing standing on it"
    if m["solid"] < 0.85:
        return False, "translucent"
    return True, "rests on a surface"


def hexc(rgb: list[int], k: float = 0.55) -> str:
    return "#" + "".join(f"{max(0, min(255, int(v * k))):02x}" for v in rgb)


def auto_radius(m: Measure) -> float:
    """Contour thickness following the figure: 1.5 units for a tiny one, 6 for
    one that fills a third of the frame."""
    share = float(m["share"])
    return round(max(1.5, min(6.0, 0.011 * float(share**0.5) * 1024)), 1)


def apply(
    svg: str,
    ms: list[Measure],
    radius: float | str | None = "auto",
    shadow: bool = True,
) -> tuple[str, dict[str, list[str]]]:
    """The finished picture and the ids that got a contour or a shadow."""
    root = ET.fromstring(svg)
    defs = root.find(Q + "defs")
    if defs is None:
        defs = ET.Element(Q + "defs")
        root.insert(0, defs)
    done: dict[str, list[str]] = {"contour": [], "shadow": []}
    for k, m in enumerate(ms):
        parent = {id(c): p for p in root.iter() for c in p}
        node = next((n for n in root.iter() if n.get("id") == m["id"]), None)
        if node is None:
            continue
        host = parent[id(node)]
        if shadow and shadow_rule(m)[0]:
            bx0, bx1, by = m["base"]
            rx = (bx1 - bx0) / 2 * 1.15 + 6
            ry = max(5.0, min(22.0, rx * 0.16))
            f = ET.SubElement(
                defs,
                Q + "filter",
                {
                    "id": f"f13-shadow-{k}",
                    "x": "-30%",
                    "y": "-80%",
                    "width": "160%",
                    "height": "260%",
                },
            )
            ET.SubElement(f, Q + "feGaussianBlur", {"stdDeviation": f"{ry * 0.45:.1f}"})
            e = ET.Element(
                Q + "ellipse",
                {
                    "id": f"f13-contact-shadow-{k}",
                    "cx": f"{(bx0 + bx1) / 2:.1f}",
                    "cy": f"{by - ry * 0.25:.1f}",
                    "rx": f"{rx:.1f}",
                    "ry": f"{ry:.1f}",
                    "fill": "#000000",
                    "opacity": "0.36",
                    "filter": f"url(#f13-shadow-{k})",
                },
            )
            # The shadow goes under the whole top-level group holding the thing.
            top = node
            while parent.get(id(top)) is not None and parent[id(top)] is not root:
                top = parent[id(top)]
            root.insert(list(root).index(top), e)
            done["shadow"].append(m["id"])
        if radius and contour_rule(m)[0]:
            width = auto_radius(m) if radius == "auto" else radius
            f = ET.SubElement(
                defs,
                Q + "filter",
                {
                    "id": f"f13-contour-{k}",
                    "filterUnits": "userSpaceOnUse",
                    "x": "-205",
                    "y": "-205",
                    "width": "1434",
                    "height": "1434",
                },
            )
            ct = ET.SubElement(
                f, Q + "feComponentTransfer", {"in": "SourceAlpha", "result": "hard"}
            )
            ET.SubElement(
                ct, Q + "feFuncA", {"type": "linear", "slope": "4", "intercept": "-1.5"}
            )
            ET.SubElement(
                f,
                Q + "feMorphology",
                {
                    "in": "hard",
                    "operator": "dilate",
                    "radius": f"{width}",
                    "result": "fat",
                },
            )
            ET.SubElement(
                f,
                Q + "feGaussianBlur",
                {"in": "fat", "stdDeviation": "0.7", "result": "soft"},
            )
            ET.SubElement(
                f, Q + "feFlood", {"flood-color": hexc(m["dark"]), "result": "ink"}
            )
            ET.SubElement(
                f,
                Q + "feComposite",
                {"in": "ink", "in2": "soft", "operator": "in", "result": "line"},
            )
            merge = ET.SubElement(f, Q + "feMerge")
            ET.SubElement(merge, Q + "feMergeNode", {"in": "line"})
            ET.SubElement(merge, Q + "feMergeNode", {"in": "SourceGraphic"})
            wrap = ET.Element(
                Q + "g", {"id": f"f13-outlined-{k}", "filter": f"url(#f13-contour-{k})"}
            )
            wrap.tail = node.tail
            host.insert(list(host).index(node), wrap)
            host.remove(node)
            wrap.append(node)
            done["contour"].append(m["id"])
    return ET.tostring(root, encoding="unicode"), done
