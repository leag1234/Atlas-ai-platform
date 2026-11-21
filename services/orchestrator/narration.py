"""Remove non-answer narration and wire material before delivery."""

from collections.abc import Awaitable, Callable
import json
import logging
import re

_PREFIXES = (
    "je vais rechercher",
    "je vais consulter",
    "je vais vérifier",
    "je vais verifier",
    "j'ai maintenant suffisamment d'éléments pour synthétiser",
    "j’ai maintenant suffisamment d’éléments pour synthétiser",
    "j'ai maintenant suffisamment d'elements pour synthetiser",
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
# One clause, no colon and no further sentence: "Let me check the math: 12 x 7
# = 84" and "Let me check. It is Canberra." are answers; the dot of "U.S."
# does not end the clause (audit 2026-11-21).
_UNFINISHED = re.compile(
    r"\s*(?:i need to|i(?:'ll| will)|let me) (?:search|look(?: this)? up|verify|check)\b"
    r"(?:[^:.!?\n]|\.(?=\S|[ \t]+(?-i:[a-zà-ÿ])))*[.!?…]*\s*",
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
        # Held characters, joined only when the object closes: appending to one
        # string per character copied it each time (quadratic on a long object).
        self.held: list[str] = []
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
            if self.backticks == 3 and not self.held:
                self.code = not self.code
            if self.code:
                output.append(char)
                continue
            if not self.held:
                if char == "{":
                    self.held, self.depth = [char], 1
                else:
                    output.append(char)
                continue
            self.held.append(char)
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
                    held = "".join(self.held)
                    if protocol_material(held):
                        self.removed.append(held)
                    else:
                        output.append(held)
                    self.held = []
        return "".join(output)

    @property
    def pending(self) -> str:
        return "".join(self.held)

    def finish(self) -> str:
        tail, self.held = self.pending, []
        if protocol_material(tail):
            self.removed.append(tail)
            return ""
        return tail


_LONGEST_PREFIX = max(len(prefix) for prefix in _PREFIXES)


class NarrationFilter:
    def __init__(self) -> None:
        # Held characters, joined only when released or removed, and the start
        # of their lower-cased text without leading spaces: recomputing both for
        # every character was quadratic on a long space run or sentence.
        self.held: list[str] = []
        self.head = ""
        self.matched = False
        self.removed: list[str] = []
        self.passing = False
        self.code = False
        self.backticks = 0

    @property
    def pending(self) -> str:
        return "".join(self.held)

    def release(self) -> str:
        held = self.pending
        self.held, self.head, self.matched = [], "", False
        return held

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
            self.held.append(char)
            # head = the held text without leading spaces, lower-cased, as far
            # as a prefix can reach: a longer head can no longer start one.
            if (self.head or not char.isspace()) and not self.matched:
                if len(self.head) <= _LONGEST_PREFIX:
                    self.head += char.lower()
                    self.matched = any(
                        self.head.startswith(prefix) for prefix in _PREFIXES
                    )
            possible = len(self.head) <= _LONGEST_PREFIX and any(
                prefix.startswith(self.head) for prefix in _PREFIXES
            )
            if self.matched:
                if char in ".!?\n":
                    self.removed.append(self.release().strip())
            elif not possible:
                output.append(self.release())
                self.passing = char not in ".!?\n"
        return "".join(output)

    def finish(self) -> str:
        tail = self.release()
        if any(tail.lstrip().lower().startswith(prefix) for prefix in _PREFIXES):
            self.removed.append(tail.strip())
            return ""
        return tail


class TurnHold:
    """Keep one model turn's streamed text back until the turn's kind is known.

    What a model writes before it calls a tool ("I found relevant results, I
    will now read...") is a working note, never answer text, and a stream cannot
    take text back. The turn's text is held, then dropped when the turn calls a
    tool, or released when the turn is the final answer.
    """

    def __init__(self, sink: Callable[[dict[str, object]], Awaitable[None]]) -> None:
        self.sink = sink
        self.events: list[dict[str, object]] = []

    async def __call__(self, event: dict[str, object]) -> None:
        self.events.append(event)

    async def release(self) -> None:
        """The turn is the answer: deliver what was held."""
        events, self.events = self.events, []
        for event in events:
            await self.sink(event)

    def drop(self) -> None:
        """The turn called a tool: its text was a working note."""
        characters = sum(
            len(content)
            for event in self.events
            if isinstance(delta := event.get("delta"), dict)
            and isinstance(content := delta.get("content"), str)
        )
        if characters:
            logging.getLogger(__name__).info(
                "working_notes_withheld characters=%d", characters
            )
        self.events = []


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
