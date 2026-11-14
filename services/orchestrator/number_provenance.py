"""Number provenance (stage 0, log only).

A number that an answer presents as the result of a computation (a total, a
difference, an average, a conversion, the right-hand side of a written
calculation) should come from evidence the turn actually holds: a tool result, a
retrieved page, an attached document, or the conversation. This module finds such
numbers, recomputes written calculations deterministically, and reports counts.
It never changes the answer. ATLAS_NUMBER_PROVENANCE=log enables it (default off).
"""

from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
import json
import logging
import os
import re

from packages.limits import LimitError
from services.orchestrator.calculator import calculate

LOGGER = logging.getLogger(__name__)
EVIDENCE_LIMIT = 512_000

# A number: grouped thousands (space, narrow space, comma or dot) or plain digits,
# with an optional decimal part.
NUMBER_TEXT = (
    r"\d{1,3}(?:[   ]\d{3})+(?:,\d+)?"
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    r"|\d{1,3}(?:\.\d{3}){2,}(?:,\d+)?"
    r"|\d+(?:[.,]\d+)?"
)
NUMBER = re.compile(rf"(?<![\w.,/])(?<!\w-)(?:{NUMBER_TEXT})(?![\w]|[.,]\d)")
OPERATOR = r"[+\-−×x*/÷]"
TERM = rf"\(?\s*(?:{NUMBER_TEXT})\s*\)?"
EXPRESSION = rf"{TERM}(?:\s*{OPERATOR}\s*{TERM})+"
CHAIN = re.compile(
    rf"(?<![\w.,])({EXPRESSION})((?:\s*(?:=|≈|~)\s*[-−+]?\s*(?:{EXPRESSION}|{TERM}))+)"
)
# Words that announce a computed value just before it, in the five UI languages.
CUE = re.compile(
    r"(?i)(?:\b(?:total\w*|in total|in all|sum|average|mean|median|difference|"
    r"net|remaining|left over|that is|i\.e\.|which (?:is|makes|gives|comes to)|"
    r"increase of|decrease of|"
    r"au total|en tout|somme|moyenne|m[ée]diane|diff[ée]rence|[ée]cart|soit|reste|"
    r"ce qui (?:fait|donne|repr[ée]sente)|hausse de|baisse de|"
    r"insgesamt|summe|durchschnitt|differenz|"
    r"en total|suma|promedio|media|diferencia|"
    r"in tutto|somma|differenza)\b|=|≈|→)"
    r"[^.\n\d]{0,25}$"
)
APPROXIMATE = re.compile(
    r"(?i)(?:≈|~|\b(?:about|around|approximately|roughly|nearly|environ|"
    r"près de|presque|ungefähr|etwa|aproximadamente|circa)\b)[^.\n\d]{0,12}$"
)
DATE_ARITHMETIC = re.compile(r"(?i)\b(?:ago|later|years? old|il y a|plus tard|ans)\b")
YEAR = re.compile(r"^(?:1[5-9]\d\d|20\d\d|2100)$")
HIDDEN = re.compile(
    r"<details>[\s\S]*?</details>|```[\s\S]*?(?:```|$)|`[^`\n]*`"
    r"|!?\[[^\]]*\]\([^)]*\)|https?://\S+|/api/\S+",
    re.IGNORECASE,
)
# "1 cup = 8 fl oz": a quantity with a unit stated equal to another unit.
CONVERSION = re.compile(r"\d[\d.,/]*\s*[^\W\d_][^=\n\d]{0,20}(?:=|≈)[\s*~]*$")
LIST_MARKER = re.compile(r"^\s*(?:[-*]\s*)?\d+[.)]\s")
SKIPPED_KEYS = frozenset({"url", "link", "reference", "credit_url", "error", "id"})


def enabled() -> bool:
    return os.environ.get("ATLAS_NUMBER_PROVENANCE", "off") == "log"


def number(token: str) -> Decimal | None:
    """Canonical value of a number written in English or European style."""
    text = re.sub(r"[   ]", "", token.strip())
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
        text = text.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3}){2,}(?:,\d+)?", text):
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def numbers(text: str) -> set[Decimal]:
    found = set()
    for match in NUMBER.finditer(text):
        value = number(match[0])
        if value is not None:
            found.add(value)
    return found


def visible(text: str) -> str:
    """Answer prose without tool details, code, links or image references."""
    return HIDDEN.sub(" ", text)


def places(token: str) -> int:
    match = re.search(r"[.,](\d+)$", token.strip())
    if match is None or re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", token.strip()):
        return 0
    return len(match[1])


def matches(stated: Decimal, decimals: int, value: Decimal, tolerance: Decimal) -> bool:
    """True when value, rounded as the answer wrote it, equals the stated number."""
    try:
        quantum = Decimal(1).scaleb(-decimals)
        rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return False
    if rounded == stated:
        return True
    return tolerance > 0 and abs(value - stated) <= abs(stated) * tolerance


def evaluate(expression: str) -> Decimal | None:
    """Recompute a written calculation with the bounded calculator, never eval."""
    parts: list[str] = []
    for piece in re.findall(rf"{NUMBER_TEXT}|{OPERATOR}|[()]", expression):
        if re.fullmatch(NUMBER_TEXT, piece):
            value = number(piece)
            if value is None:
                return None
            parts.append(format(value, "f"))
        else:
            parts.append({"×": "*", "x": "*", "÷": "/", "−": "-"}.get(piece, piece))
    try:
        return Decimal(calculate(" ".join(parts)))
    except (ValueError, LimitError, InvalidOperation, ZeroDivisionError):
        return None


@dataclass(frozen=True)
class Candidate:
    start: int
    token: str
    value: Decimal
    approximate: bool
    expected: Decimal | None = None
    conversion: bool = False


def candidates(answer: str) -> list[Candidate]:
    """Numbers the answer presents as computed, with their recomputed value if any."""
    text = visible(answer)
    found: dict[int, Candidate] = {}
    spans: list[tuple[int, int]] = []
    for chain in CHAIN.finditer(text):
        spans.append((chain.start(), chain.end()))
        sides = re.split(r"\s*(?:=|≈|~)\s*", chain[0])
        values = [
            evaluate(side) if re.search(OPERATOR, side[1:]) else None for side in sides
        ]
        offset = chain.start()
        for index, side in enumerate(sides):
            position = text.index(side, offset)
            offset = position + len(side)
            if index == 0:
                continue
            last = list(NUMBER.finditer(side))
            if values[index] is not None or not last:
                continue
            token = last[-1]
            value = number(token[0])
            if value is None:
                continue
            if re.match(r"\s*\(?\s*[-−]", side):
                value = -value
            expected = next(
                (v for v in reversed(values[:index]) if v is not None), None
            )
            start = position + token.start()
            found[start] = Candidate(
                start, token[0], value, "≈" in chain[0] or "~" in chain[0], expected
            )
    for match in NUMBER.finditer(text):
        if match.start() in found or any(a <= match.start() < b for a, b in spans):
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        before = text[max(line_start, match.start() - 40) : match.start()]
        if not CUE.search(before):
            continue
        if LIST_MARKER.match(text[line_start:]) and match.start() - line_start < 6:
            continue
        value = number(match[0])
        if value is None or value in (0, 1):
            continue
        if YEAR.match(match[0]) and not DATE_ARITHMETIC.search(
            text[match.start() : match.end() + 20]
        ):
            continue
        found[match.start()] = Candidate(
            match.start(),
            match[0],
            value,
            bool(APPROXIMATE.search(before)),
            conversion=bool(CONVERSION.search(before)),
        )
    return [found[key] for key in sorted(found)]


@dataclass
class Report:
    computed: int = 0
    evidence: int = 0
    conversation: int = 0
    conversion: int = 0
    recomputed: int = 0
    wrong: int = 0
    unsupported: int = 0

    def fields(self) -> dict[str, int]:
        return {
            "computed": self.computed,
            "evidence": self.evidence,
            "conversation": self.conversation,
            "conversion": self.conversion,
            "recomputed": self.recomputed,
            "wrong": self.wrong,
            "unsupported": self.unsupported,
        }


@dataclass
class Provenance:
    """Numbers held by this turn's evidence; bounded, never sent anywhere."""

    values: set[Decimal] = field(default_factory=set)
    characters: int = 0
    tool_results: int = 0

    def add_text(self, text: str) -> None:
        text = text[: max(0, EVIDENCE_LIMIT - self.characters)]
        self.characters += len(text)
        self.values |= numbers(text)

    def collect(self, value: object) -> None:
        """Collect numbers from a tool result; tool arguments and errors are not evidence."""
        if isinstance(value, dict):
            if value.get("error"):
                return
            for key, item in value.items():
                if key in SKIPPED_KEYS:
                    continue
                if key == "value" and isinstance(item, str):
                    parsed = number(item.lstrip("-"))
                    if parsed is not None:
                        self.values.add(-parsed if item.startswith("-") else parsed)
                    continue
                self.collect(item)
        elif isinstance(value, list):
            for item in value:
                self.collect(item)
        elif isinstance(value, str):
            self.add_text(value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            self.values.add(Decimal(str(value)))

    def observed(self, output: dict[str, object]) -> None:
        self.tool_results += 1
        self.collect(output)

    def supported(self, candidate: Candidate, pool: set[Decimal]) -> bool:
        decimals = places(candidate.token)
        tolerance = Decimal("0.01") if candidate.approximate else Decimal(0)
        stated = candidate.value
        return any(
            matches(stated, decimals, value, tolerance)
            or matches(stated, decimals, value * 100, tolerance)
            for value in pool
        )

    def report(self, answer: str, conversation: list[str]) -> Report:
        said = (
            set().union(*(numbers(text) for text in conversation))
            if conversation
            else set()
        )
        result = Report()
        for candidate in candidates(answer):
            result.computed += 1
            if candidate.expected is not None:
                if matches(
                    candidate.value,
                    places(candidate.token),
                    candidate.expected,
                    Decimal("0.01") if candidate.approximate else Decimal(0),
                ):
                    result.recomputed += 1
                else:
                    result.wrong += 1
            elif self.supported(candidate, self.values):
                result.evidence += 1
            elif self.supported(candidate, said):
                result.conversation += 1
            elif candidate.conversion:
                result.conversion += 1
            else:
                result.unsupported += 1
        return result


current: ContextVar[Provenance | None] = ContextVar("number_provenance", default=None)


def observe(output: dict[str, object]) -> dict[str, object]:
    """Record a tool result for the active turn and return it unchanged."""
    if (tracker := current.get()) is not None:
        tracker.observed(output)
    return output


def log(report: Report, task_type: str, tool_results: int) -> None:
    """Counts only: answer text and numbers never reach the log."""
    # Warning level: staging and production keep warnings only, and this
    # measurement line must reach the journal.
    LOGGER.warning(
        "number_provenance %s",
        json.dumps(
            {**report.fields(), "task_type": task_type, "tool_results": tool_results},
            sort_keys=True,
        ),
    )


def record(
    tracker: Provenance, answer: str, conversation: list[str], task: str
) -> None:
    """Log one turn's counts; a detector failure never affects the answer."""
    try:
        report = tracker.report(answer, conversation)
    except (ArithmeticError, ValueError, RecursionError):
        LOGGER.warning("number_provenance_failed")
        return
    log(report, task, tracker.tool_results)
