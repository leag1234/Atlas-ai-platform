"""Make a model's SVG safe for a native renderer and for the user's browser.

The painter's SVG is untrusted: it reaches resvg (native code) in the picture
worker and, once stored, the user's browser. Only drawing survives: no script,
no external resource (image, font, stylesheet, filter image, link), no event
attribute, no entity declaration, bounded size and element count. A document
that cannot be made safe is refused, never rendered.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ET.register_namespace("", NS)
ET.register_namespace("xlink", XLINK_NS)

MAX_SVG_BYTES = 600_000
MAX_ELEMENTS = 30_000
MAX_DEPTH = 60

# Elements removed with everything inside them.
REMOVED = frozenset(
    {
        "script",
        "foreignObject",
        "image",
        "feImage",
        "iframe",
        "audio",
        "video",
        "object",
        "embed",
        "animate",
        "animateColor",
        "animateMotion",
        "animateTransform",
        "set",
        "discard",
        "handler",
        "listener",
    }
)
_HREF_ATTRIBUTES = ("href", f"{{{XLINK_NS}}}href")
_EXTERNAL_URL = re.compile(r"url\(\s*(['\"]?)\s*(?!#)[^)]*\)", re.I)
_CSS_IMPORT = re.compile(r"@import[^;]*;?", re.I)
_CSS_EXPRESSION = re.compile(r"expression\s*\(|javascript:|-moz-binding", re.I)


class UnsafeSvg(ValueError):
    """The document cannot be rendered safely (its reason is the message)."""


def _local(tag: object) -> str:
    return tag.split("}")[-1] if isinstance(tag, str) else ""


def _clean_value(value: str) -> str | None:
    """An attribute or style value without external references, or None to drop it."""
    if _CSS_EXPRESSION.search(value):
        return None
    if _EXTERNAL_URL.search(value):
        cleaned = _EXTERNAL_URL.sub("none", value)
        return cleaned
    return value


def _clean_css(text: str) -> str:
    text = _CSS_IMPORT.sub("", text)
    text = _CSS_EXPRESSION.sub("", text)
    return _EXTERNAL_URL.sub("none", text)


def _depth(node: ET.Element, limit: int) -> int:
    deepest = 0
    stack: list[tuple[ET.Element, int]] = [(node, 1)]
    while stack:
        current, level = stack.pop()
        deepest = max(deepest, level)
        if deepest > limit:
            return deepest
        stack.extend((child, level + 1) for child in current)
    return deepest


def sanitise(svg: str) -> str:
    """The safe SVG document, re-serialised; UnsafeSvg when it cannot be made safe."""
    if len(svg.encode("utf-8")) > MAX_SVG_BYTES:
        raise UnsafeSvg("svg_too_large")
    if re.search(r"<!ENTITY", svg, re.I):
        raise UnsafeSvg("svg_entity")
    text = re.sub(r"<!DOCTYPE[^>\[]*>", "", svg, flags=re.I)
    if re.search(r"<!DOCTYPE", text, re.I):
        raise UnsafeSvg("svg_doctype")
    opening = re.search(r"<svg\b[^>]*>", text)
    if opening is None:
        raise UnsafeSvg("svg_missing")
    if "xmlns=" not in opening.group(0):
        text = text.replace("<svg", f'<svg xmlns="{NS}"', 1)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise UnsafeSvg("svg_unparseable") from None
    if root.tag != f"{{{NS}}}svg":
        raise UnsafeSvg("svg_root")
    if sum(1 for _ in root.iter()) > MAX_ELEMENTS:
        raise UnsafeSvg("svg_too_many_elements")
    if _depth(root, MAX_DEPTH) > MAX_DEPTH:
        raise UnsafeSvg("svg_too_deep")
    parents = {id(child): parent for parent in root.iter() for child in parent}
    for node in list(root.iter()):
        name = _local(node.tag)
        parent = parents.get(id(node))
        if not isinstance(node.tag, str):
            # Comments and processing instructions carry nothing to draw.
            if parent is not None:
                parent.remove(node)
            continue
        if name in REMOVED:
            if parent is not None:
                parent.remove(node)
            continue
        if name == "a":
            # A link inside a picture: keep its drawing, drop the link.
            node.tag = f"{{{NS}}}g"
        for key in list(node.attrib):
            local = _local(key).lower()
            value = node.attrib[key]
            if local.startswith("on"):
                del node.attrib[key]
            elif key in _HREF_ATTRIBUTES:
                if not value.strip().startswith("#"):
                    del node.attrib[key]
            elif local == "style":
                node.set(key, _clean_css(value))
            else:
                cleaned = _clean_value(value)
                if cleaned is None:
                    del node.attrib[key]
                elif cleaned != value:
                    node.set(key, cleaned)
        if name == "style" and node.text:
            node.text = _clean_css(node.text)
    return ET.tostring(root, encoding="unicode")
