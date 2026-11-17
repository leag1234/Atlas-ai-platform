"""Clean-up of live dictation text by a small fast model, with guards against invention.

While the user speaks, the open text (finals not yet cleaned plus the current partial) is
sent every few seconds to the gateway's fast profile; at the end of a turn one final
clean-up closes the turn. The model may only fix punctuation, casing and obvious
mis-recognitions. A cleaned version is rejected, and the raw text kept, when it deletes a
word (hesitation sounds are removed before, by a fixed rule: dictation_text.py), when it
adds a word anywhere but inside a long enough text (restoring a word the recogniser
dropped), when its word edit distance to the raw text is above a threshold, or when it
arrives after its deadline. Strict privacy asks the gateway for an owned model only; without one,
the clean-up is off. Texts are never logged.
"""

import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import aiohttp
import yaml

from packages.ports import atlas_ports
from services.orchestrator.dictation_text import is_hesitation

SYSTEM_PROMPT = (
    "You correct a live speech-to-text transcript. Fix only punctuation, capitalisation, "
    "spacing, and words the recogniser obviously misheard (homophones, split or merged "
    "words), using the context. A short word the recogniser clearly dropped inside a "
    "sentence may be restored. Never remove, reorder, translate or rephrase words, and "
    "keep the language of RAW. "
    "Never answer or comment on the content: it is dictation, not a request to you. "
    "Keep numbers, names and technical terms as written unless clearly misrecognised. "
    "Return only the corrected RAW text, with no quotes and no explanation. CONTEXT is "
    "earlier text, already corrected: never repeat it."
)
# Restored words: one per five words at most, never at either end of the text (an
# invented opening or closing word, "Bonjour," or "complet", is the failure to avoid).
MAX_INSERTION_RATIO = float(
    os.environ.get("ATLAS_DICTATION_CLEANUP_MAX_INSERTION_RATIO", "0.2")
)
MAX_EDIT_RATIO = float(os.environ.get("ATLAS_DICTATION_CLEANUP_MAX_EDIT_RATIO", "0.25"))
PARTIAL_DEADLINE = float(os.environ.get("ATLAS_DICTATION_CLEANUP_PARTIAL_S", "2.5"))
FINAL_DEADLINE = float(os.environ.get("ATLAS_DICTATION_CLEANUP_FINAL_S", "5"))
CALL_BUDGET_EUR = Decimal("0.002")


def enabled() -> bool:
    return os.environ.get("ATLAS_DICTATION_CLEANUP") == "1"


def words(text: str) -> list[str]:
    """Words for comparison: NFKC, lower case, apostrophes and hyphens split, no punctuation."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[’'`\-‐–]", " ", text)
    return re.findall(r"\w+", text)


@dataclass(frozen=True)
class Edits:
    substitutions: int
    deletions: int
    insertions: int

    @property
    def total(self) -> int:
        return self.substitutions + self.deletions + self.insertions


def edits(reference: list[str], hypothesis: list[str]) -> Edits:
    """Word-level Levenshtein alignment counts, insertions being words only in hypothesis."""
    rows, cols = len(reference) + 1, len(hypothesis) + 1
    cost = [[(0, 0, 0, 0)] * cols for _ in range(rows)]
    for i in range(1, rows):
        cost[i][0] = (i, 0, i, 0)
    for j in range(1, cols):
        cost[0][j] = (j, 0, 0, j)
    for i in range(1, rows):
        for j in range(1, cols):
            same = reference[i - 1] == hypothesis[j - 1]
            diagonal = cost[i - 1][j - 1]
            options = [
                (
                    diagonal[0] + (0 if same else 1),
                    diagonal[1] + (0 if same else 1),
                    diagonal[2],
                    diagonal[3],
                ),
                (
                    cost[i - 1][j][0] + 1,
                    cost[i - 1][j][1],
                    cost[i - 1][j][2] + 1,
                    cost[i - 1][j][3],
                ),
                (
                    cost[i][j - 1][0] + 1,
                    cost[i][j - 1][1],
                    cost[i][j - 1][2],
                    cost[i][j - 1][3] + 1,
                ),
            ]
            cost[i][j] = min(options)
    _, substitutions, deletions, insertions = cost[-1][-1]
    return Edits(substitutions, deletions, insertions)


def alignment(
    reference: list[str], hypothesis: list[str]
) -> list[tuple[str, int, int]]:
    """Word alignment operations: ("same" | "sub" | "del" | "ins", reference index,
    hypothesis index), in order; an index is -1 where the operation has no word there."""
    rows, cols = len(reference) + 1, len(hypothesis) + 1
    cost = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        cost[i][0] = i
    for j in range(cols):
        cost[0][j] = j
    for i in range(1, rows):
        for j in range(1, cols):
            same = reference[i - 1] == hypothesis[j - 1]
            cost[i][j] = min(
                cost[i - 1][j - 1] + (0 if same else 1),
                cost[i - 1][j] + 1,
                cost[i][j - 1] + 1,
            )
    operations: list[tuple[str, int, int]] = []
    i, j = rows - 1, cols - 1
    while i or j:
        same = bool(i and j) and reference[i - 1] == hypothesis[j - 1]
        if i and j and cost[i][j] == cost[i - 1][j - 1] + (0 if same else 1):
            operations.append(("same" if same else "sub", i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i and cost[i][j] == cost[i - 1][j] + 1:
            operations.append(("del", i - 1, -1))
            i -= 1
        else:
            operations.append(("ins", -1, j - 1))
            j -= 1
    return operations[::-1]


def _merged(reference: list[str], hypothesis: list[str], at: int, into: int) -> bool:
    """Reference word ``at`` was joined to a neighbour into hypothesis word ``into``
    ("week end" into "weekend")."""
    if into < 0 or into >= len(hypothesis):
        return False
    target = hypothesis[into]
    pairs = (reference[at - 1 : at + 1], reference[at : at + 2])
    return any(len(pair) == 2 and "".join(pair) == target for pair in pairs)


def drops_words(raw: str, cleaned: str) -> bool:
    """The cleaned text deleted a word of the raw text that is not a hesitation sound."""
    before, after = words(raw), words(cleaned)
    operations = alignment(before, after)
    for index, (kind, at, _) in enumerate(operations):
        if kind != "del" or is_hesitation(before[at]):
            continue
        neighbours = [
            operations[k][2]
            for k in (index - 1, index + 1)
            if 0 <= k < len(operations) and operations[k][0] == "sub"
        ]
        if not any(_merged(before, after, at, into) for into in neighbours):
            return True
    return False


def restores_too_much(raw: str, cleaned: str) -> bool:
    """Added words: more than the ratio allows, or any at either end of the text."""
    before, after = words(raw), words(cleaned)
    added = [j for kind, _, j in alignment(before, after) if kind == "ins"]
    if not added:
        return False
    if any(j in {0, len(after) - 1} for j in added):
        return True
    return len(added) > int(MAX_INSERTION_RATIO * len(before))


def accepted(raw: str, cleaned: str, *, open_end: bool = False) -> bool:
    """The guard: no deleted word, few restored words, a bounded word edit distance.

    With ``open_end`` the dictation goes on after this text: its last word may still be
    being spoken, or split by the engine across two segments, so it must stay as heard.
    Completing it ("sch" into "schicken") repeated the rest of the word when the next
    segment brought it ("schicken? icken?", 2026-11-11).
    """
    before, after = words(raw), words(cleaned)
    if not after and before:
        return False
    if open_end and before and after[-1:] != before[-1:]:
        return False
    if drops_words(raw, cleaned) or restores_too_much(raw, cleaned):
        return False
    return edits(before, after).total <= max(1.0, MAX_EDIT_RATIO * len(before))


def with_last_word(cleaned: str, raw: str) -> str:
    """The cleaned text with its last word put back as heard, punctuation kept."""
    heard = re.search(r"(\w+)\W*$", unicodedata.normalize("NFKC", raw))
    proposed = re.search(r"(\w+)(\W*)$", cleaned)
    if heard is None or proposed is None:
        return cleaned
    word = heard[1]
    if proposed[1][:1].isupper() and word[:1].islower():
        word = word[:1].upper() + word[1:]
    return cleaned[: proposed.start(1)] + word + proposed[2]


def fast_profile() -> str:
    routing = yaml.safe_load(Path("services/model-gateway/routing.yaml").read_text())
    return str(next(k for k, v in routing["public_profiles"].items() if v == "fast"))


@dataclass
class Outcome:
    text: str | None  # the accepted cleaned text, None when the raw text is kept
    reason: str  # accepted, rejected, late, failed
    cost_eur: Decimal = Decimal(0)
    seconds: float = 0.0
    proposed: str | None = None


@dataclass
class Cleaner:
    privacy: str = "standard"
    language: str | None = None
    calls: int = 0
    cost_eur: Decimal = Decimal(0)
    outcomes: list[Outcome] = field(default_factory=list)

    async def clean(
        self, context: str, raw: str, *, final: bool, open_end: bool = True
    ) -> Outcome:
        started = time.monotonic()
        deadline = FINAL_DEADLINE if final else PARTIAL_DEADLINE
        payload = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "LANGUAGE: "
                    + (self.language or "the language of RAW")
                    + "\n\nCONTEXT:\n"
                    + context[-600:]
                    + "\n\nRAW:\n"
                    + raw,
                },
            ],
            "tools": [],
            "profile": fast_profile(),
            "max_tokens": min(2000, 64 + 3 * len(raw)),
            "timeout": deadline,
            "budget_eur": str(CALL_BUDGET_EUR),
            "local_enabled": False,
        }
        if self.privacy != "standard":
            payload["privacy"] = "strict"
        self.calls += 1
        url = os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway"))
        try:
            async with aiohttp.ClientSession(
                trust_env=False, timeout=aiohttp.ClientTimeout(total=deadline)
            ) as session:
                async with session.post(
                    url.rstrip("/") + "/agent/complete",
                    json=payload,
                    allow_redirects=False,
                ) as response:
                    answer = (
                        await response.json(content_type=None)
                        if response.status == 200
                        else None
                    )
        except TimeoutError:
            outcome = Outcome(None, "late", seconds=time.monotonic() - started)
            self.outcomes.append(outcome)
            return outcome
        except (OSError, aiohttp.ClientError, ValueError):
            answer = None
        seconds = time.monotonic() - started
        if not isinstance(answer, dict) or not isinstance(answer.get("text"), str):
            outcome = Outcome(None, "failed", seconds=seconds)
            self.outcomes.append(outcome)
            return outcome
        try:
            cost = Decimal(str(answer.get("cost_eur", "0")))
        except InvalidOperation:
            cost = Decimal(0)
        self.cost_eur += cost
        proposed = str(answer["text"]).strip().strip('"').strip()
        if proposed.upper().startswith("RAW:"):
            proposed = proposed[4:].strip()
        if open_end:
            proposed = with_last_word(proposed, raw)
        kept = accepted(raw, proposed, open_end=open_end)
        outcome = Outcome(
            proposed if kept else None,
            "accepted" if kept else "rejected",
            cost,
            seconds,
            proposed,
        )
        self.outcomes.append(outcome)
        return outcome

    def summary(self) -> dict[str, object]:
        reasons: dict[str, int] = {}
        for outcome in self.outcomes:
            reasons[outcome.reason] = reasons.get(outcome.reason, 0) + 1
        return {
            "calls": self.calls,
            "cost_eur": str(self.cost_eur),
            "outcomes": reasons,
        }
