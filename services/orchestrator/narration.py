"""Remove non-answer narration and wire material before delivery."""

import json
import logging
import re

_PREFIXES = (
    "je vais rechercher",
    "je vais consulter",
    "je vais vérifier",
    "je vais verifier",
    "je peux maintenant synthétiser",
    "je peux maintenant synthetiser",
    "j'ai une source détaillée",
    "j’ai une source détaillée",
    "let me verify",
    "let me check",
    "let me search",
    "let me try one more targeted search",
    "i will search",
    "i will now search",
    "i'll search",
    "i need to search",
    "i will look up",
    "the search results don't",
    "i've now run",
    "i have now run",
    "i can now summarize",
)

_SCOPE = re.compile(r'"scope"\s*:\s*"[^"}]*?(?:current request|requete actuelle)', re.I)
_TOOL_HISTORY = re.compile(r'"untrusted_tool_history"\s*:', re.I)
_RAW_TOOL_CALL = re.compile(r'"name"\s*:\s*"[^"}]+"\s*,\s*"arguments"\s*:', re.I)
_FENCED_CODE = re.compile(r"```.*?```", re.S)
_UNFINISHED = re.compile(
    r"\s*(?:i need to|i(?:'ll| will)|let me) (?:search|look(?: this)? up|verify|check)\b.*[.!?]?\s*",
    re.I,
)


def protocol_material(text: str) -> bool:
    """Whether delivered text contains internal request or tool wire material."""
    delivered = _FENCED_CODE.sub("", text)
    return bool(
        _SCOPE.search(delivered)
        or _TOOL_HISTORY.search(delivered)
        or _RAW_TOOL_CALL.search(delivered)
    )


def unfinished_announcement(text: str) -> bool:
    """Whether text promises research but supplies no answer."""
    return bool(_UNFINISHED.fullmatch(text))


def raw_tool_call(text: str, offered: set[str]) -> tuple[str, str] | None:
    """Parse a bare, offered function call which the model emitted as answer text."""
    try:
        value = json.loads(text.strip())
    except ValueError:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"name", "arguments"}
        or not isinstance(value.get("name"), str)
        or value["name"] not in offered
        or not isinstance(value.get("arguments"), dict)
    ):
        return None
    return value["name"], json.dumps(value["arguments"], ensure_ascii=False)


class ProtocolFilter:
    """Hold non-code JSON objects until they can be proven safe for delivery."""

    def __init__(self) -> None:
        self.pending = ""
        self.depth = 0
        self.quote = False
        self.escape = False
        self.code = False
        self.backticks = 0
        self.removed: list[str] = []

    def feed(self, text: str) -> str:
        output: list[str] = []
        for char in text:
            self.backticks = self.backticks + 1 if char == "`" else 0
            if self.backticks == 3 and not self.pending:
                self.code = not self.code
            if self.code:
                output.append(char)
                continue
            if not self.pending:
                if char == "{":
                    self.pending, self.depth = char, 1
                else:
                    output.append(char)
                continue
            self.pending += char
            if self.quote:
                if self.escape:
                    self.escape = False
                elif char == "\\":
                    self.escape = True
                elif char == '"':
                    self.quote = False
                continue
            if char == '"':
                self.quote = True
            elif char == "{":
                self.depth += 1
            elif char == "}":
                self.depth -= 1
                if self.depth == 0:
                    if protocol_material(self.pending):
                        self.removed.append(self.pending)
                    else:
                        output.append(self.pending)
                    self.pending = ""
        return "".join(output)

    def finish(self) -> str:
        tail, self.pending = self.pending, ""
        if protocol_material(tail):
            self.removed.append(tail)
            return ""
        return tail


class NarrationFilter:
    def __init__(self) -> None:
        self.pending = ""
        self.removed: list[str] = []
        self.passing = False
        self.code = False
        self.backticks = 0

    def feed(self, text: str) -> str:
        output = []
        for char in text:
            self.backticks = self.backticks + 1 if char == "`" else 0
            if self.backticks == 3:
                self.code = not self.code
            if self.passing or self.code:
                output.append(char)
                if char in ".!?\n" and not self.code:
                    self.passing = False
                continue
            self.pending += char
            candidate = self.pending.lstrip().lower()
            match = any(candidate.startswith(prefix) for prefix in _PREFIXES)
            possible = any(prefix.startswith(candidate) for prefix in _PREFIXES)
            if match:
                if char in ".!?\n":
                    self.removed.append(self.pending.strip())
                    self.pending = ""
            elif not possible:
                output.append(self.pending)
                self.pending = ""
                self.passing = char not in ".!?\n"
        return "".join(output)

    def finish(self) -> str:
        tail, self.pending = self.pending, ""
        if any(tail.lstrip().lower().startswith(prefix) for prefix in _PREFIXES):
            self.removed.append(tail.strip())
            return ""
        return tail


def clean_answer(text: str, *, allow_empty: bool = False) -> str:
    parser = NarrationFilter()
    answer = parser.feed(text) + parser.finish()
    if parser.removed:
        logging.getLogger(__name__).info(
            "narration_removed sentences=%d", len(parser.removed)
        )
    if text.strip() and not answer.strip() and not allow_empty:
        raise ValueError("empty_content_after_narration")
    return answer.lstrip() if parser.removed else answer
