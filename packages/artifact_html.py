"""Deterministic validation for self-contained HTML artefacts."""

from __future__ import annotations

import html.parser
import os
import re
from dataclasses import dataclass

from packages.text_deliverables import TextDeliverableError, validate_text

DEFAULT_MAX_BYTES = 262144

URL_ATTRIBUTES = frozenset(
    {
        "src",
        "href",
        "srcset",
        "action",
        "formaction",
        "poster",
        "data",
        "background",
        "xlink:href",
        "ping",
        "manifest",
        "cite",
        "longdesc",
        "icon",
    }
)

FORBIDDEN_ELEMENTS = frozenset(
    {"iframe", "frame", "frameset", "object", "embed", "portal", "base"}
)

ALLOWED_NAMESPACES = frozenset(
    {
        "http://www.w3.org/2000/svg",
        "http://www.w3.org/1999/xhtml",
        "http://www.w3.org/1999/xlink",
        "http://www.w3.org/1998/Math/MathML",
        "http://www.w3.org/XML/1998/namespace",
    }
)

NETWORK_API_PATTERNS = (
    "fetch(",
    "XMLHttpRequest",
    "WebSocket(",
    "EventSource(",
    "sendBeacon(",
    "importScripts(",
    "import(",
)

NAVIGATION_API_PATTERNS = (
    "window.open(",
    "top.location",
    "parent.location",
    "location.assign(",
    "location.replace(",
    "location.reload(",
)

DIALOG_API_PATTERNS = (
    "alert(",
    "confirm(",
    "prompt(",
)

EXTERNAL_URL_PATTERN = re.compile(
    r"(?:https?|wss?)://[^\s'\"<>]+|(?<![\w:/])//[^\s'\"<>]+", re.IGNORECASE
)
STYLE_URL_PATTERN = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.IGNORECASE)
STYLE_IMPORT_PATTERN = re.compile(r"@import\s+['\"]([^'\"]+)['\"]", re.IGNORECASE)
STYLE_IMPORT_URL_PATTERN = re.compile(
    r"@import\s+url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.IGNORECASE
)
LOCATION_ASSIGNMENT_PATTERN = re.compile(
    r"(?<![\w.])(?:location|location\.href)\s*=[^=]", re.IGNORECASE
)


@dataclass(frozen=True)
class Problem:
    """A validation failure with a stable code and source line."""

    code: str
    line: int


class _Validator(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problems: list[Problem] = []
        self._saw_html = False
        self._saw_body = False
        self._script_lines: list[tuple[int, int]] = []
        self._script_start = 1
        self._script_text: list[str] = []
        self._in_script = False
        self._style_start = 1
        self._style_text: list[str] = []
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        line = self.getpos()[0]
        if tag == "html":
            self._saw_html = True
        if tag == "body":
            self._saw_body = True
        if tag in FORBIDDEN_ELEMENTS:
            self.problems.append(Problem("forbidden_element", line))
        if tag == "meta":
            attributes = self._attribute_map(attrs)
            if attributes.get("http-equiv", "").lower() == "refresh":
                self.problems.append(Problem("forbidden_element", line))
        if tag == "link":
            href = self._attribute_map(attrs).get("href", "")
            if not href.startswith("data:"):
                self.problems.append(Problem("forbidden_element", line))
        if tag == "form":
            action = self._attribute_map(attrs).get("action", "")
            if action:
                self.problems.append(Problem("form_action", line))
            attrs = [(name, value) for name, value in attrs if name.lower() != "action"]
        if tag == "script":
            self._script_start = line
            self._script_text = []
            self._in_script = True
        elif tag == "style":
            self._style_start = line
            self._style_text = []
            self._in_style = True
        self._check_attributes(tag, attrs, line)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script":
            self._check_script()
            self._in_script = False
        elif tag.lower() == "style":
            self._check_style("".join(self._style_text), self._style_start)
            self._style_text = []
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._script_text.append(data)
        if self._in_style:
            self._style_text.append(data)

    @staticmethod
    def _attribute_map(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {name.lower(): value or "" for name, value in attrs}

    def _check_attributes(
        self, tag: str, attrs: list[tuple[str, str | None]], line: int
    ) -> None:
        for name, raw_value in attrs:
            name = name.lower()
            value = raw_value or ""
            if name in URL_ATTRIBUTES:
                if name == "srcset":
                    for candidate in value.split(","):
                        url = candidate.strip().split(" ", 1)[0]
                        self._check_url_attribute(url, line)
                else:
                    self._check_url_attribute(value, line)
            if name == "style":
                self._check_style(value, line)

    def _check_url_attribute(self, value: str, line: int) -> None:
        if not value or value.startswith(("#", "data:", "blob:", "javascript:")):
            return
        if value.startswith("//") or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value):
            self.problems.append(Problem("external_reference", line))
            return
        if tag_is_relative(value):
            self.problems.append(Problem("relative_reference", line))

    def _check_style(self, value: str, line: int) -> None:
        for match in STYLE_URL_PATTERN.finditer(value):
            url = match.group(1).strip()
            if not url.startswith(("data:", "#")):
                self.problems.append(Problem("external_reference", line))
        for match in STYLE_IMPORT_PATTERN.finditer(value):
            url = match.group(1).strip()
            if not url.startswith(("data:", "#")):
                self.problems.append(Problem("external_reference", line))
        for match in STYLE_IMPORT_URL_PATTERN.finditer(value):
            url = match.group(1).strip()
            if not url.startswith(("data:", "#")):
                self.problems.append(Problem("external_reference", line))

    def _check_script(self) -> None:
        if not self._script_text:
            return
        text = "".join(self._script_text)
        for pattern in NETWORK_API_PATTERNS:
            self._find_script_problem(text, pattern, "network_api")
        for pattern in NAVIGATION_API_PATTERNS:
            self._find_script_problem(text, pattern, "navigation_api")
        for pattern in DIALOG_API_PATTERNS:
            self._find_script_problem(text, pattern, "dialog_api")
        for match in EXTERNAL_URL_PATTERN.finditer(text):
            if match.group(0) not in ALLOWED_NAMESPACES:
                self._add_script_problem(match.start(), "external_reference")
        for match in LOCATION_ASSIGNMENT_PATTERN.finditer(text):
            self._add_script_problem(match.start(), "navigation_api")
        self._script_text = []

    def _find_script_problem(self, text: str, pattern: str, code: str) -> None:
        start = text.find(pattern)
        if start >= 0:
            self._add_script_problem(start, code)

    def _add_script_problem(self, start: int, code: str) -> None:
        line = self._script_start + "".join(self._script_text).count("\n", 0, start)
        self.problems.append(Problem(code, line))

    def finalize(self) -> list[Problem]:
        self.close()
        if self._in_script:
            self._check_script()
        if self._in_style:
            self._check_style("".join(self._style_text), self._style_start)
        if not self._saw_html or not self._saw_body:
            self.problems.append(Problem("not_a_document", 1))
        return sorted(self.problems, key=lambda problem: (problem.line, problem.code))


def tag_is_relative(value: str) -> bool:
    """Return whether an attribute value is a relative file reference."""
    return not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value)


def validate(content: str) -> list[Problem]:
    """Validate a self-contained HTML document and return all problems."""
    max_bytes = int(os.environ.get("ATLAS_ARTIFACT_MAX_BYTES", DEFAULT_MAX_BYTES))
    try:
        text = validate_text(content.encode("utf-8"), ".html")
    except TextDeliverableError as error:
        return [Problem(error.code, 1)]
    if len(text.encode("utf-8")) > max_bytes:
        return [Problem("too_large", 1)]

    parser = _Validator()
    parser.feed(text)
    return parser.finalize()


# Models sometimes write attribute values without quotes. Two forms break SVG
# drawings (staging 2026-11-10): an unquoted value right before "/>" swallows
# the slash (stroke-width=2/> reads as "2/", the element stays open and the
# following shapes are lost), and an unquoted viewBox keeps only its first
# number. Rejecting made the model resend the same HTML until the loop guard
# stopped the turn, so Atlas quotes those values itself before validation.
_UNQUOTED_VIEWBOX = re.compile(
    r"(\bviewBox=)(-?[\d.]+(?:[\s,]+-?[\d.]+){3})(?=[\s/>])", re.IGNORECASE
)
_UNQUOTED_BEFORE_CLOSE = re.compile(r"(<[A-Za-z][^<>]*?\s[\w:.-]+=)([^\s\"'<>=`/]+)/>")


def quote_attributes(text: str) -> str:
    """Quote the unquoted attribute values that break SVG parsing."""
    text = _UNQUOTED_VIEWBOX.sub(lambda m: m[1] + '"' + m[2] + '"', text)
    previous = None
    while previous != text:
        previous = text
        text = _UNQUOTED_BEFORE_CLOSE.sub(lambda m: m[1] + '"' + m[2] + '"/>', text)
    return text
