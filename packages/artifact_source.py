"""The app or drawing file a model writes as a fenced block in its answer.

The file is never carried inside tool-call arguments: long content in JSON
arguments failed on escaping, was cut by the output limit and was not streamed
(staging 2026-11-11, 8 of 18 corpus apps). The model writes ```html or ```svg in
its ordinary answer; Atlas takes the last complete block of that type, validates
it and stores it.
"""

from __future__ import annotations

import html
import re
import unicodedata
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from typing import Literal

from packages.artifact_html import Problem, quote_attributes, validate

Kind = Literal["html", "svg"]

DRAWING_MARKER = '<meta name="atlas-kind" content="drawing">'
_OPENING = re.compile(r"^[ \t]{0,3}(`{3,})[ \t]*([A-Za-z0-9_+-]*)[^\n`]*$")
_HTML_LANGUAGES = frozenset({"html", "htm", "xhtml"})
_SVG_LANGUAGES = frozenset({"svg"})
_GENERIC_LANGUAGES = frozenset({"", "xml", "code", "text"})
_RAW_HTML = re.compile(r"(?is)(<!doctype html\b.*?</html\s*>|<html\b.*?</html\s*>)")
_RAW_SVG = re.compile(r"(?is)<svg\b.*?</svg\s*>")
_TITLE = re.compile(r"(?is)<title[^>]*>(.*?)</title\s*>")
_FORBIDDEN_XML = re.compile(r"(?i)<!(?:doctype|entity)")
_BARE_AMPERSAND = re.compile(r"&(?!#?[A-Za-z0-9]+;)")


@dataclass(frozen=True)
class Block:
    kind: Kind
    content: str
    complete: bool


def _kind(language: str, content: str) -> Kind | None:
    language = language.casefold()
    if language in _HTML_LANGUAGES:
        return "html"
    if language in _SVG_LANGUAGES:
        return "svg"
    if language not in _GENERIC_LANGUAGES:
        return None
    start = content.lstrip()[:400].casefold()
    if start.startswith(("<!doctype html", "<html")):
        return "html"
    if start.startswith("<svg") or (start.startswith("<?xml") and "<svg" in start):
        return "svg"
    return None


def blocks(text: str) -> list[Block]:
    """Fenced html and svg blocks in order; the last one may be unterminated."""
    found: list[Block] = []
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        opening = _OPENING.match(lines[index])
        if opening is None:
            index += 1
            continue
        fence, language = opening.group(1), opening.group(2)
        closing = re.compile(rf"^[ \t]{{0,3}}`{{{len(fence)},}}[ \t]*$")
        body: list[str] = []
        index += 1
        complete = False
        while index < len(lines):
            if closing.match(lines[index]):
                complete = True
                index += 1
                break
            body.append(lines[index])
            index += 1
        content = "\n".join(body)
        kind = _kind(language, content)
        if kind is not None:
            found.append(Block(kind, content, complete))
    return found


def last_block(text: str) -> Block | None:
    """The last complete html or svg block; an unfenced document as a fallback."""
    complete = [block for block in blocks(text) if block.complete]
    if complete:
        return complete[-1]
    if "```" in text:
        return None
    documents = list(_RAW_HTML.finditer(text))
    if documents:
        return Block("html", documents[-1].group(1), True)
    drawings = list(_RAW_SVG.finditer(text))
    if drawings:
        return Block("svg", drawings[-1].group(0), True)
    return None


def prose(text: str) -> str:
    """The answer without its code: fenced blocks and unfenced documents removed."""
    kept: list[str] = []
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        opening = _OPENING.match(lines[index])
        if opening is None:
            kept.append(lines[index])
            index += 1
            continue
        closing = re.compile(rf"^[ \t]{{0,3}}`{{{len(opening.group(1))},}}[ \t]*$")
        index += 1
        while index < len(lines) and not closing.match(lines[index]):
            index += 1
        index += 1
    remaining = "\n".join(kept)
    remaining = _RAW_HTML.sub("", remaining)
    remaining = _RAW_SVG.sub("", remaining)
    return re.sub(r"\n{3,}", "\n\n", remaining).strip()


class LineCounter:
    """Lines of the file being written, read from the streamed answer."""

    def __init__(self) -> None:
        self.text = ""
        self.inside = False
        self.lines = 0
        self._scanned = 0
        self._fence = ""

    def feed(self, chunk: str) -> int:
        self.text += chunk
        while True:
            end = self.text.find("\n", self._scanned)
            if end < 0:
                break
            line = self.text[self._scanned : end]
            self._scanned = end + 1
            if self.inside:
                if re.match(rf"^[ \t]{{0,3}}`{{{len(self._fence)},}}[ \t]*$", line):
                    self.inside = False
                else:
                    self.lines += 1
            else:
                opening = _OPENING.match(line)
                if opening is not None:
                    self.inside = True
                    self._fence = opening.group(1)
                    self.lines = 0
        return self.lines


def title_of(content: str) -> str:
    match = _TITLE.search(content)
    if match is None:
        return ""
    title = html.unescape(re.sub(r"<[^>]*>", "", match.group(1)))
    title = re.sub(r"[\s\[\]]+", " ", title).strip()
    return title[:80].strip()


def filename_for(title: str, default: str) -> str:
    ascii_title = (
        unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.casefold()).strip("-")[:60]
    slug = slug.strip("-")
    return (slug or default) + ".html"


def is_drawing(content: str) -> bool:
    """A stored file made from an SVG drawing (or an older script-free SVG page)."""
    folded = content.casefold()
    return DRAWING_MARKER in content or ("<svg" in folded and "<script" not in folded)


def drawing_source(content: str) -> str:
    """The SVG element of a stored drawing, for a modification turn."""
    match = _RAW_SVG.search(content)
    return match.group(0) if match else content


def svg_problems(svg: str) -> tuple[str, list[Problem]]:
    """Parse an SVG drawing; bare ampersands are escaped, entities are refused."""
    svg = quote_attributes(svg.strip())
    if svg.startswith("<?xml"):
        svg = svg[svg.find("?>") + 2 :].lstrip()
    if _FORBIDDEN_XML.search(svg):
        return svg, [Problem("svg_doctype", 1)]
    svg = _BARE_AMPERSAND.sub("&amp;", svg)
    try:
        root = ElementTree.fromstring(svg)
    except ElementTree.ParseError as error:
        line = error.position[0] if error.position else 1
        return svg, [Problem("svg_syntax", line)]
    if root.tag.rsplit("}", 1)[-1] != "svg":
        return svg, [Problem("svg_root", 1)]
    return svg, []


def _with_view_box(svg: str) -> str:
    opening = re.match(r"(?is)<svg\b[^>]*>", svg)
    if opening is None or re.search(r"(?i)\bviewBox\s*=", opening.group(0)):
        return svg
    width = re.search(r'(?i)\bwidth\s*=\s*"([\d.]+)(?:px)?"', opening.group(0))
    height = re.search(r'(?i)\bheight\s*=\s*"([\d.]+)(?:px)?"', opening.group(0))
    if width is None or height is None:
        return svg
    return (
        svg[:4]
        + f' viewBox="0 0 {width.group(1)} {height.group(1)}"'
        + svg[4 : opening.end()]
        + svg[opening.end() :]
    )


def drawing_document(svg: str, title: str, language: str) -> str:
    """A self-contained page showing one SVG drawing, scaled to the panel."""
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{html.escape(language)}">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{DRAWING_MARKER}\n<title>{html.escape(title)}</title>\n"
        "<style>html,body{margin:0;height:100%;background:#fff}"
        "body{display:flex;align-items:center;justify-content:center}"
        "body>svg{display:block;width:100%;height:auto;max-height:100vh}</style>\n"
        f"</head>\n<body>\n{_with_view_box(svg)}\n</body>\n</html>\n"
    )


@dataclass(frozen=True)
class Prepared:
    document: str
    problems: list[Problem]
    # The text the problem line numbers refer to (the SVG or the HTML file).
    source: str


def prepare(block: Block, title: str, language: str) -> Prepared:
    """The HTML file to store for a block, and its validation problems."""
    if block.kind == "svg":
        svg, problems = svg_problems(block.content)
        if problems:
            return Prepared("", problems, svg)
        document = drawing_document(svg, title or title_of(svg), language)
        return Prepared(document, validate(document), document)
    document = quote_attributes(block.content.strip())
    return Prepared(document, validate(document), document)
