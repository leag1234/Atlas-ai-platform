"""Recover the SVG or JSON a model wrote in its answer, without another call.

The salvage repairs only what the painter commonly gets wrong at the XML level
(comments, bare ampersands, duplicate attributes, a document cut by the output
limit); it never edits the drawing.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

from packages import linear_scan

# Spans to a closer: a failed attempt means no closer follows, so every later
# opening fails too and the scan stops (the former search reread the rest of
# the answer from each opening: quadratic).
_SVG = linear_scan.Scan(
    re.compile(r"<svg\b.*</svg>", re.S),
    re.compile(r"<svg\b"),
    linear_scan.nothing_after,
)
_JSON_OBJECT = linear_scan.Scan(
    re.compile(r"\{.*\}", re.S), re.compile(r"\{"), linear_scan.nothing_after
)
_COMMENTS = linear_scan.Scan(
    re.compile(r"<!--.*?-->", re.S), re.compile(r"<!--"), linear_scan.nothing_after
)


def _opening_tags(name: str) -> linear_scan.Scan:
    """Opening tags of an element that are not self-closing; a failed one reads to
    the first ">", which every later opening before it reaches too."""
    return linear_scan.Scan(
        re.compile(rf"<{name}\b[^>]*?(?<!/)>"),
        re.compile(rf"<{name}\b"),
        linear_scan.up_to(r">"),
    )


_OPEN_SVG = re.compile(r"<svg\b.*", re.S)
_ATTRIBUTE = re.compile(r'\s([A-Za-z_:][\w:.-]*)\s*=\s*("[^"]*"|\'[^\']*\')')
_TAG = re.compile(r"<[A-Za-z][^<>]*>")
_BARE_AMPERSAND = re.compile(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")
# Containers whose unfinished opening tag is dropped when the answer was cut.
_CUT_CONTAINERS = (
    "linearGradient",
    "radialGradient",
    "filter",
    "clipPath",
    "defs",
    "text",
)
_CUT_OPENINGS = {name: _opening_tags(name) for name in ("g", *_CUT_CONTAINERS)}


def extract_svg(text: str | None) -> str | None:
    """The SVG document of an answer, or its unfinished start when it was cut."""
    found = _SVG.search(text or "")
    if found:
        return found.group(0)
    opened = _OPEN_SVG.search(text or "")
    return opened.group(0).split("```")[0] if opened else None


def extract_json(text: str | None) -> dict[str, object] | None:
    """The first JSON object of an answer, or None."""
    found = _JSON_OBJECT.search(text or "")
    if not found:
        return None
    try:
        value = json.loads(found.group(0))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _dedupe_attributes(match: re.Match[str]) -> str:
    tag = match.group(0)
    seen: set[str] = set()
    out: list[str] = []
    position = 0
    for attribute in _ATTRIBUTE.finditer(tag):
        out.append(tag[position : attribute.start()])
        position = attribute.end()
        if attribute.group(1) not in seen:
            seen.add(attribute.group(1))
            out.append(attribute.group(0))
    out.append(tag[position:])
    return "".join(out)


def _parses(svg: str) -> bool:
    try:
        ET.fromstring(svg)
    except ET.ParseError:
        return False
    return True


def salvage(svg: str | None) -> tuple[str | None, str]:
    """(well-formed SVG or None, what was done: "", "salvaged", "truncated_closed"
    or "no_svg" / "unparseable ...")."""
    if svg is None:
        return None, "no_svg"
    if _parses(svg):
        return svg, ""
    fixed = _COMMENTS.sub("", svg)
    fixed = _BARE_AMPERSAND.sub("&amp;", fixed)
    fixed = _TAG.sub(_dedupe_attributes, fixed)
    if "xlink:" in fixed and "xmlns:xlink" not in fixed:
        fixed = fixed.replace(
            "<svg", '<svg xmlns:xlink="http://www.w3.org/1999/xlink"', 1
        )
    if _parses(fixed):
        return fixed, "salvaged"
    note = ""
    if "</svg>" not in fixed:
        # Cut by the output limit: drop the unfinished tag, close open groups.
        cut = fixed[: fixed.rfind(">") + 1]
        for name in _CUT_CONTAINERS:
            opened = len(list(_CUT_OPENINGS[name].finditer(cut)))
            if opened > len(re.findall(rf"</{name}>", cut)):
                cut = cut[: cut.rfind(f"<{name}")]
        # Opening tags that are not self-closing (an attribute-less <g> too).
        depth = len(list(_CUT_OPENINGS["g"].finditer(cut))) - len(
            re.findall(r"</g>", cut)
        )
        cut += "</g>" * max(depth, 0) + "</svg>"
        try:
            ET.fromstring(cut)
            return cut, "truncated_closed"
        except ET.ParseError as error:
            note = str(error)
    return None, "unparseable " + note[:120]
