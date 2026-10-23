"""Deterministic, dependency-free recovery for Mermaid emitted by the model."""

import logging
import re
from functools import lru_cache

FENCE = re.compile(
    r"(?m)^[ \t]*(?P<fence>`{3,}|~{3,})mermaid[^\S\n]*\n"
    r"(?P<source>[\s\S]*?)(?:\n[ \t]*(?P=fence)[ \t]*(?=\n|$)|\Z)",
    re.I,
)
_LOG = logging.getLogger(__name__)
_FLOW_HEADER = re.compile(r"^\s*(?:flowchart|graph)\b", re.I)
_KNOWN_HEADERS = re.compile(
    r"^\s*(?:flowchart|graph|sequenceDiagram|classDiagram|stateDiagram(?:-v2)?|erDiagram|pie|gantt|mindmap|timeline)\b",
    re.I,
)
_NODE = re.compile(r"\b[A-Za-z_][\w-]*(?P<open>\[\[|\[\(|\(\[|\(\(|\{\{|\[|\(|\{)")
_CLOSERS = {
    "[[": "]]",
    "[(": ")]",
    "([": "])",
    "((": "))",
    "{{": "}}",
    "[": "]",
    "(": ")",
    "{": "}",
}
_ARROW = re.compile(r"(?:<-->|<--|-->|---|-.->|==>|<==>|<==|--[ox]|[ox]--)")
_SAFE_FLOW_LINE = re.compile(
    r"^\s*(?:subgraph\b|end\s*$|direction\b|classDef\b|class\b|style\b|linkStyle\b|click\b|%%|[A-Za-z_][\w-]*(?:\s*(?:\[\[|\[\(|\(\[|\(\(|\{\{|\[|\(|\{)))",
    re.I,
)
_DANGEROUS_LABEL = re.compile(r"[()\[\]{}\"|\n]|\\n")
_EMOJI = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\ufe0f\u200d]")


def _end_of_label(source: str, start: int, opening: str) -> int | None:
    closing = _CLOSERS[opening]
    if source.startswith('"', start):
        found = source.find('"' + closing, start + 1)
        return found + 1 if found >= 0 else None
    depth, end = len(opening), start
    while end < len(source):
        if source[end] == opening[-1]:
            depth += 1
        elif source[end] == closing[0]:
            depth -= 1
            if depth == 0:
                return end - len(closing) + 1
        end += 1
    # Model labels frequently contain an unmatched display bracket. On an edge,
    # the final shape terminator before the connector is the only safe fallback.
    boundary = source.find("-->", start)
    if boundary < 0:
        boundary = source.find("\n", start)
    if boundary < 0:
        boundary = len(source)
    found = source.rfind(closing, start, boundary)
    return found if found >= start else None


def _quoted(label: str, strip_emoji: bool) -> str:
    label = label.replace("\\n", "<br/>").replace("\n", "<br/>")
    if strip_emoji:
        label = _EMOJI.sub("", label)
    return (
        label.strip()
        .replace('"', "#quot;")
        .replace("[", "#91;")
        .replace("]", "#93;")
        .replace("{", "#123;")
        .replace("}", "#125;")
        .replace("|", "#124;")
    )


def _repair_subgraphs(source: str, strip_emoji: bool) -> str:
    pattern = re.compile(
        r"(?ms)^(?P<prefix>\s*subgraph\s+[A-Za-z_][\w-]*\s*)\[(?P<label>.*?)\](?=\s*$)"
    )

    def replace(match: re.Match[str]) -> str:
        label = match["label"]
        if not _DANGEROUS_LABEL.search(label) and not (
            strip_emoji and _EMOJI.search(label)
        ):
            return match[0]
        return f'{match["prefix"]}["{_quoted(label, strip_emoji)}"]'

    return pattern.sub(replace, source)


@lru_cache(maxsize=256)
def repair(source: str, strip_emoji: bool = False) -> str:
    """Quote only flowchart labels whose contents need Mermaid escaping."""
    if not _FLOW_HEADER.match(source):
        return source
    source = _repair_subgraphs(source, strip_emoji)
    chunks: list[str] = []
    cursor = 0
    while match := _NODE.search(source, cursor):
        opening = match["open"]
        closing = _CLOSERS[opening]
        start = match.end()
        end = _end_of_label(source, start, opening)
        if end is None:
            chunks.append(source[cursor : match.end()])
            cursor = match.end()
            continue
        label = source[start:end]
        raw = label[1:-1] if label.startswith('"') and label.endswith('"') else label
        if not _DANGEROUS_LABEL.search(raw) and not (
            strip_emoji and _EMOJI.search(raw)
        ):
            chunks.append(source[cursor : end + len(closing)])
        else:
            chunks.append(
                source[cursor:start] + '"' + _quoted(raw, strip_emoji) + '"' + closing
            )
        cursor = end + len(closing)
    return "".join(chunks) + source[cursor:]


def _balanced(line: str) -> bool:
    stack: list[str] = []
    pairs = {"[": "]", "(": ")", "{": "}"}
    quoted = escaped = False
    for char in line:
        if quoted:
            if char == '"' and not escaped:
                quoted = False
            escaped = char == "\\" and not escaped
        elif char == '"':
            quoted = True
        elif char in pairs:
            stack.append(pairs[char])
        elif char in "])}" and (not stack or stack.pop() != char):
            return False
    return not quoted and not stack


def _valid_flowchart(source: str) -> bool:
    for line in source.splitlines()[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("%%"):
            continue
        arrows = list(_ARROW.finditer(stripped))
        if arrows:
            pieces = _ARROW.split(stripped)
            if any(not piece.strip() for piece in pieces):
                return False
        elif not _SAFE_FLOW_LINE.match(stripped):
            return False
    return True


def _valid(source: str) -> bool:
    lines = source.splitlines()
    first = next((line for line in lines if line.strip()), "")
    if not _KNOWN_HEADERS.match(first):
        # Extension headers (for example gitGraph) are intentionally opaque.
        # Bare prose is not a Mermaid diagram and must use the one bounded retry.
        return bool(re.fullmatch(r"\s*[A-Za-z][\w-]*\s*", first))
    if any(not _balanced(line) for line in lines):
        return False
    return not _FLOW_HEADER.match(first) or _valid_flowchart(source)


async def valid(sources: list[str]) -> list[bool]:
    """Check known Mermaid families without invoking a runtime parser."""
    return [_valid(source) for source in sources]


async def checked(text: str) -> tuple[str, list[str]]:
    """Repair each fenced block; return rejected blocks for one model retry."""
    matches = list(FENCE.finditer(text))
    if not matches:
        return text, []
    sources = [repair(match["source"]) for match in matches]
    states = await valid(sources)
    for index, state in enumerate(states):
        if not state:
            sources[index] = repair(sources[index], True)
            states[index] = (await valid([sources[index]]))[0]
    failed: list[str] = []
    parts: list[str] = []
    cursor = 0
    for match, source, state in zip(matches, sources, states):
        parts.append(text[cursor : match.start()])
        if state:
            parts.append(
                match[0]
                if source == match["source"]
                and match[0].rstrip().endswith(match["fence"])
                else f"```mermaid\n{source}\n```"
            )
        else:
            failed.append(match[0])
            _LOG.warning("mermaid_rejected reason=invalid_syntax")
        cursor = match.end()
    return "".join(parts) + text[cursor:], failed
