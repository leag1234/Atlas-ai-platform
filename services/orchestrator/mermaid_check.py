"""Bounded validation with the UI's Mermaid parser, never parser diagnostics."""

import asyncio
import json
import logging
from pathlib import Path
import re
from functools import lru_cache

FENCE = re.compile(
    r"(?m)^[ \t]*(?P<fence>`{3,}|~{3,})mermaid[^\S\n]*\n"
    r"(?P<source>[\s\S]*?)(?:\n[ \t]*(?P=fence)[ \t]*(?=\n|$)|\Z)",
    re.I,
)
_LOG = logging.getLogger(__name__)
_SCRIPT = Path(__file__).with_name("mermaid_parser") / "check.mjs"
_CACHE: dict[str, bool] = {}


async def valid(sources: list[str]) -> list[bool]:
    missing = list(dict.fromkeys(source for source in sources if source not in _CACHE))
    if missing:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                "node",
                "--max-old-space-size=192",
                str(_SCRIPT),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            output, _ = await asyncio.wait_for(
                process.communicate(json.dumps(missing).encode()),
                timeout=5,
            )
            results = json.loads(output)
            if (
                process.returncode
                or not isinstance(results, list)
                or len(results) != len(missing)
                or any(type(r) is not bool for r in results)
            ):
                raise ValueError("parser_protocol")
        except (OSError, ValueError, TimeoutError):
            _LOG.warning("mermaid_rejected reason=parser_unavailable")
            return [False for _ in sources]
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
        if len(_CACHE) + len(missing) > 256:
            _CACHE.clear()
        _CACHE.update(zip(missing, results))
    return [_CACHE[source] for source in sources]


@lru_cache(maxsize=256)
def repair(source: str, strip_emoji: bool = False) -> str:
    """Quote balanced legacy flowchart node labels, leaving other grammars alone."""
    if not re.match(r"\s*(?:flowchart|graph)\b", source):
        return source
    node = re.compile(r"\b[A-Za-z_][\w-]*(?P<open>\[\[|\[\(|\(\[|\(\(|\{\{|\[|\(|\{)")
    closers = {
        "[[": "]]",
        "[(": ")]",
        "([": "])",
        "((": "))",
        "{{": "}}",
        "[": "]",
        "(": ")",
        "{": "}",
    }
    chunks: list[str] = []
    cursor = 0
    while match := node.search(source, cursor):
        opening = match["open"]
        closing = closers[opening]
        start = match.end()
        end = start
        # A quoted label has a well-defined terminator even with punctuation inside.
        if source[start : start + 1] == '"':
            found = source.find('"' + closing, start + 1)
            end = found + 1 if found >= 0 else len(source)
        else:
            depth = 0
            while end < len(source):
                if depth == 0 and source.startswith(closing, end):
                    break
                if source[end] == opening[-1]:
                    depth += 1
                elif source[end] == closing[0] and depth:
                    depth -= 1
                end += 1
        if end >= len(source):
            chunks.append(source[cursor : match.end()])
            cursor = match.end()
            continue
        label = source[start:end]
        if label.startswith('"') and label.endswith('"'):
            label = label[1:-1]
        label = label.replace("\\n", "<br/>").replace("\n", "<br/>")
        if strip_emoji:
            label = re.sub(
                "[\U0001f000-\U0001faff\u2600-\u27bf\ufe0f\u200d]", "", label
            )
        label = label.replace('"', "#quot;").replace("[", "#91;").replace("]", "#93;")
        chunks.append(source[cursor:start] + '"' + label.strip() + '"' + closing)
        cursor = end + len(closing)
    return "".join(chunks) + source[cursor:]


async def checked(text: str) -> tuple[str, list[str]]:
    """Repair and parse each block; return removed blocks for one bounded retry."""
    matches = list(FENCE.finditer(text))
    if not matches:
        return text, []
    sources = [repair(m["source"]) for m in matches]
    states = await valid(sources)
    for strip in (False, True):
        pending = [i for i, state in enumerate(states) if not state]
        if not pending:
            break
        repaired = [repair(sources[i], strip) for i in pending]
        for i, source, state in zip(pending, repaired, await valid(repaired)):
            sources[i], states[i] = source, state
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
