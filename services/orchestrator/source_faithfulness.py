"""An answer or a file derived from a provided source is checked against it.

Owner rule (2026-11-16): before saying a deliverable is done, Atlas checks what
it produced against the request; a mismatch gets one repair, then the answer
says honestly what is still missing. An integrator using the OpenAI-compatible
API reported a document summary that added a causal link the source never
stated. This module is that check for anything that restates a source the
person provided: a summary, a synthesis, a report, a rewrite or translation, an
answer about an attached or pasted document.

When it runs: the turn holds a provided source (an attached document, or a
pasted message of at least document_quotes.PASTED_MINIMUM characters; passages
a search retrieved from an indexed corpus are not one) and the answer is a
finished chat or API answer (not a project, Work or nearby turn; Work files go through
deliverable_check.review, which calls ``judge`` here). No keyword of a
deliverable type is read: whether the answer presents content as the source's
is decided by the judge, which is given the request.

What it does (T8, code checks, the model decides):
1. code compares every number of the answer (amounts, years, days, counts) by
   value with the numbers of the source, whatever their separators. Free; it
   sees no relation, so its result is only used when the judge cannot give a
   verdict (failure, source too long): the answer then names the numbers the
   source lacks. Passed to the judge as a note, the misses of a lookup of names
   and whole dates made it flag 4 of 12 faithful outputs (staging smoke
   2026-11-17), so the judge reads the measured prompt alone;
2. one judge call reads the whole source, the request and the answer, lists every
   link the answer states through a connector (cause, temporal order, condition,
   negation, contrast, quantity, attribution) with whether the source states it,
   then every unsupported statement. It runs first on the fast screen profile
   (role "source_faithfulness_screen", no reasoning): an answer it finds clean is
   done; what it flags is judged again, same prompt, on the profile of the
   "source_faithfulness" role, whose verdict alone is used (a cascade: cheap
   screen, precise confirmation; FrugalGPT, Chen et al. 2023). A screen that
   fails or is not enabled leaves the decision to that judge alone;
3. code checks every quotation the judge returns against the answer: a statement
   whose words are not in the answer is located by word overlap or dropped (it
   cannot be a defect of this answer), and a link the judge calls stated whose
   words are not in the answer is not confirmed (verdict not_found);
4. an answer not yet sent (API, JSON, or a held stream) with unsupported
   statements gets one bounded rewrite of the flagged sentences only, from the
   source, and is checked again; what is still unsupported is named in a short
   note in the answer's language. An answer already streamed to the interface
   cannot be replaced: the note follows it.

Survey and measurement (pilot of 2026-11-17, six public texts in six languages,
66 outputs with one injected fault each or none): code alone found 15 of 42
faults (numbers, dates, names) with no false alarm; claim decomposition with
passage retrieval found 42 of 42 but rejected 11 of 24 faithful outputs; the
one-call judge with the connector step (prompt below) found 42 of 42 with 0 false
alarms in 72 readings of the 24 faithful outputs, p50 27 s, p95 41 s, about
0.006 EUR per output, on the profile bound to the "source_faithfulness" role.
Staging smoke of this module's prompt (request added, 2026-11-17, same model):
6/6 added causal links found on the right sentence, 0/12 faithful outputs
flagged, p50 25 s, p95 34 s; a repair with its re-check took 44 to 69 s.
Related work: FActScore (Min et al. 2023), SummaC (Laban et al. 2022), AlignScore
(Zha et al. 2023), Chain-of-Verification (Dhuliawala et al. 2023), CRITIC (Gou
et al. 2024: critique is reliable when grounded in tool output).

Latency: no call on a turn without a provided source. With one, the judge
alone added p50 25 s, p95 34 to 41 s, about 0.006 EUR for a source of 1 000
words (input grows with the source, bounded by SOURCE_LIMIT and
JUDGE_BUDGET_EUR): its model cannot turn reasoning off (lowest effort "low",
2 000 to 3 700 output tokens, nearly all reasoning). Latency measurement of
2026-11-17 (pilot, 66 outputs; 98 of the 264 outputs of the larger set): the
screen alone answers in p50 1 to 1.5 s, p95 2 to 3 s, but flagged 5 of 56
faithful readings of the pilot and 3 of 56 of the larger set. Screen then
judge: 0 false alarm (0/24, 0/56); faults found 41 to 42 of 42 on the pilot,
39 of 42 on the larger set where the judge alone finds 40 (an added cause
carried by a participle clause passes the screen); faithful answers are
checked in p50 1.0 to 1.6 s, p95 2 to 26 s, flagged ones in p50 26 to 35 s;
about 0.004 to 0.006 EUR per clean answer, 0.01 per flagged one. A repair and
its re-check add 20 to 45 s more. In the interface the answer streams as
before; the stream closes after the check (keepalive frames meanwhile) and a
note may follow. API answers and held streams wait for the check. A
whole-answer judge cannot run while the answer is still being written;
checking paragraph by paragraph during the stream would multiply calls and was
not measured.

Never blocking, never disguised (D5): a judge that fails, times out or is
refused by a limit leaves the answer as it is; the failure and its code are
logged, and the answer never claims a check that did not happen. A source longer
than SOURCE_LIMIT is not judged (the pilot measured short documents only; a long
one needs passage retrieval first): only code's number lookup applies, and the
log says so.

ATLAS_SOURCE_FAITHFULNESS: "on" (default) checks, repairs and notes; "log"
checks and logs counts only, the answer unchanged; "off" disables it. Logs carry
counts and codes only, never source or answer text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable

from packages import privacy

LOGGER = logging.getLogger(__name__)

MODES = frozenset({"on", "log", "off"})
# The judge reads the whole source: beyond this it is not judged (see above).
SOURCE_LIMIT = 80_000
REQUEST_CHARACTERS = 1_500
# An answer of fewer words states nothing to check.
MIN_WORDS = 4
JUDGE_SECONDS = 60.0
# The screen answers in p50 1.5 s, p95 3 s (2026-11-17): past this it is
# abandoned for the judge.
SCREEN_SECONDS = 20.0
# The assistant path refuses more than 6,000 output tokens (output_budget).
JUDGE_TOKENS = 6_000
JUDGE_BUDGET_EUR = Decimal("0.04")
REWRITE_SECONDS = 45.0
REWRITE_TOKENS = 4_000
REWRITE_BUDGET_EUR = Decimal("0.02")
# Under this, a call has no time left to be useful.
MIN_SECONDS = 12.0
# A rewrite is asked only with this much time left after it for its re-check.
RECHECK_SECONDS = 30.0
# Kept free before the request deadline for the delivery itself.
MARGIN_SECONDS = 5.0
MAX_FINDINGS = 12
MAX_HINTS = 20
# A paraphrased statement is located in the answer sentence holding this share
# of its words (character pairs for scripts written without spaces).
OVERLAP = 0.6
NOTE_ITEM_CHARACTERS = 240

JUDGE = (
    "Compare the SUMMARY with the SOURCE. The SUMMARY is an assistant's answer "
    "to the REQUEST, written from the SOURCE: a summary, a report, a rewrite or "
    "translation, or an answer about it. Check every statement the summary "
    "presents as what the source says, shows or implies. Text the request asked "
    "the assistant to create beyond the source (a new story, a draft reply, "
    "advice, opinions, questions to the person) is not checked, but any fact it "
    "attributes to the source is.\n"
    "Step 1. List every link the summary states between two facts through a "
    "connector or a construction: cause or consequence (because, since, so, "
    "therefore, thanks to, as a result, which allowed, a participle clause "
    "giving a reason; donc, car, grâce à, ainsi, étant; weil, deshalb, daher, "
    "dadurch; porque, por lo que, por eso; perché, quindi, per questo motivo; "
    "ので, ため, このため, から), temporal order (before, after, then; avant, "
    "après, puis; vor, nach, danach; antes, después; prima, dopo; 前, 後), "
    "condition, negation, contrast, quantity (only, all, none) and attribution "
    "(according to, decided by). For each link, decide whether the source states "
    "that link itself or implies it directly. Two facts that the source only "
    "mentions side by side do not support a cause between them.\n"
    "Step 2. List every statement in the summary that is not supported by the "
    "source: wrong or unsupported facts, numbers, dates, names, and every link "
    "from step 1 that the source does not state. Paraphrase is fine.\n"
    'Output only JSON: {"links":[{"text":"<summary words, copied exactly>",'
    '"source_states_it":true}],"unsupported":[{"text":"<summary statement, '
    'copied exactly>","reason":"..."}]} with an empty "unsupported" list if '
    "everything is supported.\n"
)
REWRITE = (
    "You correct sentences of an assistant's answer that a check against the "
    "SOURCE found unsupported. You receive the numbered sentences, for each what "
    "the check found, and the SOURCE.\n"
    "For each sentence, state only what the source states: replace a wrong fact, "
    "number, date or name with the source's value; remove a link (cause, order, "
    "condition, contrast, quantity, attribution) that the source does not state "
    "and keep the facts the source does state, side by side; remove what the "
    "source does not support at all. Never add anything the source does not "
    "state. Keep the sentence's language, its markdown (list marker, bold, "
    "links) and every other word. If the source does support the sentence as "
    "written, return it unchanged; if nothing correct remains, return an empty "
    "string.\n"
    "Return only a JSON object mapping each sentence number to its corrected "
    'text, for example {"1": "corrected sentence", "2": ""}.'
)
NOTE = {
    "en": "Checked against the provided document: not supported by it: {items}.",
    "fr": "Vérifié sur le document fourni, sans appui dans ce document : {items}.",
    "de": "Am bereitgestellten Dokument geprüft, dort nicht belegt: {items}.",
    "es": "Comprobado con el documento proporcionado, sin respaldo en él: {items}.",
    "it": "Verificato sul documento fornito, senza riscontro in esso: {items}.",
}
# The judge could not run: only what code measured is said.
DETAILS_NOTE = {
    "en": "Not found in the provided document: {items}.",
    "fr": "Introuvable dans le document fourni : {items}.",
    "de": "Im bereitgestellten Dokument nicht gefunden: {items}.",
    "es": "No encontrado en el documento proporcionado: {items}.",
    "it": "Non trovato nel documento fornito: {items}.",
}

_SENTENCE_END = re.compile(r"[.!?](?=\s)|[。！？]|\n")
_NO_SPACES = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
_LINK = re.compile(r"https?://[^\s)>\]]++|\]\([^)\s]++\)")
# A written number, not glued to Latin letters ("C8", "A380" are names); next
# to characters of a script written without spaces it is still a number.
_NUMBER = re.compile(
    r"(?<![A-Za-z\d.,])(?:\d{1,3}(?:[ \u00a0\u202f.,]\d{3})+|\d+)(?:[.,]\d+)?"
    r"(?![A-Za-z\d])"
)
# The number of a numbered list item ("1. ", "2) ", "**3.** "): structure, not a fact.
_ORDINAL = re.compile(r"[ \t]*+(?:[*_]{1,2})?\d{1,3}[.)]")


def _line_start(text: str, index: int) -> int:
    return text.rfind("\n", 0, index) + 1


# How a number is named in a note: with its percent sign or the word after it.
_SHOWN = re.compile(
    r"(?:\d{1,3}(?:[ \u00a0\u202f.,]\d{3})+|\d+)(?:[.,]\d+)?"
    r"(?:\s?(?:%|[^\W\d_]{4,11}\b))?"
)
# A remark in brackets inside a quotation (the judge's own explanation).
_REMARK = re.compile(r"\([^()]{0,200}\)|（[^（）]{0,200}）")

Complete = Callable[[dict[str, Any], float], Awaitable[dict[str, Any]]]


def mode() -> str:
    value = os.environ.get("ATLAS_SOURCE_FAITHFULNESS", "on").strip().lower()
    return value if value in MODES else "on"


def enabled() -> bool:
    return mode() != "off"


@dataclass
class Source:
    """The provided source of a turn, as the judge reads it."""

    text: str
    # The texts the details are looked up in, and the person's own short
    # messages (their words are theirs, not added details).
    passages: list[str]
    own: list[str]


def provided(sources: Any) -> Source | None:
    """The source a turn holds (document_quotes.Sources), or None without one."""
    from services.orchestrator.document_quotes import PASTED_MINIMUM

    if sources is None:
        return None
    # Passages a search retrieved from an indexed corpus are not a provided
    # source: the answer was not written from them as a whole, and checking
    # them was neither asked nor measured.
    documents = getattr(sources, "provided", [])
    if not documents and not any(
        len(text) >= PASTED_MINIMUM for text in sources.conversation
    ):
        return None
    parts: list[str] = []
    passages: list[str] = []
    for index, (text, name) in enumerate(documents, start=1):
        parts.append(f"[Document {index}{': ' + name if name else ''}]\n{text}")
        passages.append(text)
    own: list[str] = []
    for text in sources.conversation:
        if len(text) >= PASTED_MINIMUM:
            parts.append("[Text the person pasted in the conversation]\n" + text)
            passages.append(text)
        else:
            own.append(text)
    if own:
        parts.append("[The person's other messages]\n" + "\n".join(own))
    if sources.evidence:
        # Tool results of the turn (a page read, a calculation) are sources too.
        parts.append("[Tool results of this turn]\n" + "\n".join(sources.evidence))
        passages.extend(sources.evidence)
    return Source("\n\n".join(parts), passages, own)


@dataclass
class Finding:
    """A statement of the answer the check found unsupported."""

    text: str
    reason: str
    kind: str  # unsupported, link, not_confirmed
    start: int = -1
    end: int = -1
    exact: bool = False


@dataclass
class Verdict:
    findings: list[Finding] = field(default_factory=list)
    links: int = 0
    # Judge quotations found nowhere in the answer, and links called stated
    # whose words are not in the answer (not confirmed, kept as findings).
    unlocated: int = 0
    not_confirmed: int = 0
    cost_eur: Decimal = Decimal(0)
    seconds: float = 0.0
    # The fast screen: clean, flagged, failed:<code>, or empty when it did not run.
    screen: str = ""


@dataclass
class Report:
    mode: str = "off"
    outcome: str = (
        ""  # clean, revised, noted, revised_noted, failed:<code>, skipped:<reason>
    )
    hints: int = 0
    findings: int = 0
    findings_after: int = 0
    links: int = 0
    unlocated: int = 0
    not_confirmed: int = 0
    rewrite: str = ""  # kept, rejected:<reason>, failed:<code>, skipped:<reason>
    screen: list[str] = field(default_factory=list)  # one per check (Verdict.screen)
    calls: int = 0
    cost_eur: Decimal = Decimal(0)
    seconds: float = 0.0
    judge_seconds: list[float] = field(default_factory=list)
    source_characters: int = 0

    def metadata(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "outcome": self.outcome,
            "hints": self.hints,
            "findings": self.findings,
            "findings_after": self.findings_after,
            "links": self.links,
            "unlocated": self.unlocated,
            "not_confirmed": self.not_confirmed,
            "rewrite": self.rewrite,
            "screen": self.screen,
            "calls": self.calls,
            "cost_eur": str(self.cost_eur),
            "seconds": round(self.seconds, 2),
            "judge_seconds": [round(s, 2) for s in self.judge_seconds],
            "source_characters": self.source_characters,
        }


class JudgeFailure(Exception):
    """The judge gave no usable verdict; code names why (D5: never hidden)."""

    def __init__(self, code: str, cost_eur: Decimal = Decimal(0)) -> None:
        super().__init__(code)
        self.code = code
        self.cost_eur = cost_eur


# ------------------------------------------------------------------ code checks


def sentences(body: str) -> list[tuple[int, int]]:
    """Sentence and line spans of a text, in order."""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_END.finditer(body):
        if match[0] == ".":
            words = body[max(start, match.start() - 6) : match.start()].split()
            word = words[-1].strip("(\"'*_[") if words else ""
            following = body[match.end() : match.end() + 3].lstrip()
            if (
                len(word) == 1
                or not following
                or following[0].isdigit()
                or following[0].islower()
            ):
                # "J. Smith", "No. 2", "etc. and": the sentence goes on.
                continue
        if body[start : match.end()].strip():
            spans.append((start, match.end()))
        start = match.end()
    if body[start:].strip():
        spans.append((start, len(body)))
    return spans


def _tokens(canonical_text: str) -> set[str]:
    found: set[str] = set()
    for word in canonical_text.split():
        if _NO_SPACES.search(word):
            found.update(word[i : i + 2] for i in range(max(1, len(word) - 1)))
        else:
            found.add(word)
    return found


def _in_order(parts: list[str], text: str) -> bool:
    position = 0
    for part in parts:
        found = text.find(part, position)
        if found < 0:
            return False
        position = found + len(part)
    return True


def locate(
    quote: str, body: str, spans: list[tuple[int, int]]
) -> tuple[int, int, bool] | None:
    """The answer sentence(s) holding a quotation: (start, end, verbatim).

    Verbatim means the same words in the same order once formatting is removed
    (document_quotes.canonical); a gap marker ("...", "…", "[...]") may join
    pieces, and a remark in brackets that the answer does not hold is the
    judge's own. Otherwise the sentence holding at least OVERLAP of its words;
    None when no sentence does.
    """
    from services.orchestrator.document_quotes import canonical, fragments

    wanted = canonical(quote)
    if not wanted or not spans:
        return None
    texts = [canonical(body[start:end]) for start, end in spans]
    pieces = [
        fragments(quote)[0],
        fragments(_REMARK.sub(" ", quote))[0],
    ]
    # Its words in the answer's order, some left out ("A, ... B" written
    # without the marker): the judge read these words.
    pieces.append(wanted.split())
    if _NO_SPACES.search(wanted):
        # A script written without spaces: its characters, in order.
        pieces.append(list(wanted.replace(" ", "")))
    for parts in pieces:
        if not parts:
            continue
        for size in (1, 2, 3):
            for index in range(len(spans) - size + 1):
                if _in_order(parts, " ".join(texts[index : index + size])):
                    return spans[index][0], spans[index + size - 1][1], True
    tokens = _tokens(wanted)
    if not tokens:
        return None
    best, score = -1, 0.0
    for index, text in enumerate(texts):
        share = len(tokens & _tokens(text)) / len(tokens)
        if share > score:
            best, score = index, share
    if best < 0 or score < OVERLAP:
        return None
    return spans[best][0], spans[best][1], False


def hints(answer: str, source: Source) -> list[str]:
    """Numbers of the answer (amounts, years, days, counts) the source lacks.

    Each written number is compared by value with the numbers of the source and
    of the person's messages, whatever the separators ("3 000", "3,000",
    "3000"), so a date or an amount in another written form still matches. Only
    numbers: on the pilot outputs, names and whole dates looked up by code
    flagged 11 of 24 faithful summaries (capitalised nouns, words without
    spaces, a date written as a range), numbers none.
    """
    from bisect import bisect_left

    from services.orchestrator.unsourced_details import _number_value

    def values(texts: list[str]) -> list[float]:
        return sorted(
            parsed[0]
            for text in texts
            for match in _NUMBER.finditer(text)
            if (parsed := _number_value(text[match.start() : match.end() + 16]))
        )

    known = values([*source.passages, *source.own, time.strftime("%Y-%m-%d")])
    body = answer.split("<details>", 1)[0]
    kept: list[str] = []
    for match in _NUMBER.finditer(body):
        ordinal = _ORDINAL.match(body, _line_start(body, match.start()))
        if ordinal is not None and ordinal.end() == match.end() + 1:
            continue
        shown = _SHOWN.match(body, match.start())
        text = shown[0].strip() if shown else match[0]
        parsed = _number_value(body[match.start() : match.end() + 16])
        if parsed is None:
            continue
        value, precision = parsed
        tolerance = precision / 2 + abs(value) * 1e-9
        index = bisect_left(known, value - tolerance)
        if index < len(known) and known[index] <= value + tolerance:
            continue
        if text not in kept:
            kept.append(text)
    return kept[:MAX_HINTS]


# ------------------------------------------------------------------ the judge


def judge_messages(request: str, source: str, answer: str) -> list[dict[str, str]]:
    content = (
        JUDGE
        + "\nREQUEST:\n"
        + request[:REQUEST_CHARACTERS]
        + "\n\nSOURCE:\n"
        + source
        + "\n\nSUMMARY:\n"
        + answer
    )
    return [{"role": "user", "content": content}]


def read_verdict(reply: str, body: str) -> Verdict | None:
    """The judge's reply checked against the answer; None when unreadable."""
    from packages.model_json import object_content

    text = reply.strip()
    opening, closing = text.find("{"), text.rfind("}")
    try:
        value = object_content(text[opening : closing + 1] if opening >= 0 else text)
    except ValueError:
        return None
    listed = value.get("unsupported")
    links = value.get("links", [])
    if not isinstance(listed, list) or not isinstance(links, list):
        return None
    spans = sentences(body)
    verdict = Verdict()
    found: dict[tuple[int, int], Finding] = {}

    def add(text: str, reason: str, kind: str) -> None:
        place = locate(text, body, spans)
        if place is None:
            verdict.unlocated += 1
            return
        start, end, exact = place
        if (start, end) in found:
            known = found[(start, end)]
            if reason and reason not in known.reason:
                known.reason = (known.reason + "; " + reason).strip("; ")
            return
        found[(start, end)] = Finding(
            " ".join(text.split())[:NOTE_ITEM_CHARACTERS],
            " ".join(reason.split())[:200],
            kind,
            start,
            end,
            exact,
        )

    for item in listed:
        if (
            isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and item["text"].strip()
        ):
            add(item["text"], str(item.get("reason") or ""), "unsupported")
    for item in links:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        verdict.links += 1
        stated = item.get("source_states_it")
        if stated is False:
            add(item["text"], "link not stated by the source", "link")
        elif stated is True:
            place = locate(item["text"], body, spans)
            if place is not None and not place[2]:
                # A link said to be stated, quoted in words the answer does not
                # hold: the verdict is not confirmed (not_found).
                verdict.not_confirmed += 1
                add(item["text"], "link not confirmed by the check", "not_confirmed")
    verdict.findings = sorted(found.values(), key=lambda f: f.start)[:MAX_FINDINGS]
    return verdict


def _cost(reply: dict[str, Any]) -> Decimal:
    try:
        spent = Decimal(str(reply.get("cost_eur") or 0))
    except InvalidOperation:
        return Decimal(0)
    return spent if spent.is_finite() and spent > 0 else Decimal(0)


async def _complete(payload: dict[str, Any], seconds: float) -> dict[str, Any]:
    from packages.ports import atlas_ports
    from services.orchestrator.model import GatewayModel

    return await GatewayModel.post(
        os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway")).rstrip("/")
        + "/agent/complete",
        payload,
        seconds,
    )


def profiles(turn: str | None) -> tuple[str | None, str | None]:
    """The judge's and the rewrite's profiles (routing.yaml picture_roles).

    Strict privacy keeps the turn's own profile; a role whose profile is not
    enabled here falls back to it as well.
    """
    if privacy.owned_only():
        return turn, turn
    from packages.profiles import picture_profile

    judge = picture_profile("source_faithfulness") or turn
    rewrite = picture_profile("source_faithfulness_rewrite") or turn
    return judge, rewrite


def screen_profile(turn: str | None) -> str | None:
    """The fast screen's profile (role source_faithfulness_screen), or None.

    None in strict privacy (the judge already runs on the turn's own profile)
    and when the role's profile is not enabled here or is the judge's own.
    """
    if privacy.owned_only():
        return None
    from packages.profiles import picture_profile

    screen = picture_profile("source_faithfulness_screen")
    judge_profile, _ = profiles(turn)
    return screen if screen is not None and screen != judge_profile else None


def _payload(
    messages: list[dict[str, str]],
    profile: str,
    tokens: int,
    seconds: float,
    budget: Decimal,
    *,
    json_mode: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "messages": messages,
        "tools": [],
        "profile": profile,
        "max_tokens": tokens,
        "timeout": seconds,
        "budget_eur": str(budget),
        "sampling_temperature": 0.0,
        "single_attempt": True,
        "local_enabled": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if privacy.owned_only():
        payload["privacy"] = "strict"
    return payload


def _failure_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    if isinstance(error, TimeoutError):
        return "timeout"
    return type(error).__name__


async def judge(
    request: str,
    source: Source,
    answer: str,
    profile: str,
    seconds: float,
    *,
    complete: Complete | None = None,
    json_mode: bool = True,
) -> Verdict:
    """One judge call on the whole source; raises JudgeFailure when unusable.

    ``json_mode`` asks the provider for a JSON object. The reasoning judge goes
    without it: on its model JSON mode made 1.5 times the output tokens for the
    same verdicts (measured 2026-11-18); the reply's object is read from the text.
    """
    started = time.monotonic()
    body = answer.split("<details>", 1)[0]
    payload = _payload(
        judge_messages(request, source.text, body),
        profile,
        JUDGE_TOKENS,
        seconds,
        JUDGE_BUDGET_EUR,
        json_mode=json_mode,
    )
    try:
        reply = await (complete or _complete)(payload, seconds + 5)
    except Exception as error:  # noqa: BLE001 - reported by code, never raised on
        raise JudgeFailure(_failure_code(error)) from error
    cost = _cost(reply) if isinstance(reply, dict) else Decimal(0)
    text = reply.get("text") if isinstance(reply, dict) else None
    if not isinstance(text, str):
        raise JudgeFailure("no_text", cost)
    verdict = read_verdict(text, body)
    if verdict is None:
        raise JudgeFailure("unreadable", cost)
    verdict.cost_eur = cost
    verdict.seconds = time.monotonic() - started
    return verdict


def check_seconds(screen: str | None) -> float:
    """The time a check may take: the screen's share first, then the judge's whole.

    The judge's time is never shortened by the screen's (measured 2026-11-18:
    sharing one budget cost the cascade 9 of 168 faults through timeouts).
    """
    return JUDGE_SECONDS + (SCREEN_SECONDS if screen is not None else 0.0)


async def checked(
    request: str,
    source: Source,
    answer: str,
    profile: str,
    screen: str | None,
    seconds: float,
    *,
    complete: Complete | None = None,
) -> Verdict:
    """The screen first; what it flags is judged on ``profile``, whose verdict is used.

    Raises JudgeFailure when the judge has to decide and gives no usable verdict
    (the screen's cost is carried by the failure).
    """
    started = time.monotonic()
    spent = Decimal(0)
    outcome = ""
    if screen is not None:
        try:
            first = await judge(
                request,
                source,
                answer,
                screen,
                min(SCREEN_SECONDS, seconds),
                complete=complete,
            )
        except JudgeFailure as failure:
            spent, outcome = failure.cost_eur, "failed:" + failure.code
        else:
            if not first.findings:
                first.screen = "clean"
                return first
            spent, outcome = first.cost_eur, "flagged"
    left = seconds - (time.monotonic() - started)
    if left < MIN_SECONDS:
        raise JudgeFailure("deadline", spent)
    try:
        verdict = await judge(
            request,
            source,
            answer,
            profile,
            min(JUDGE_SECONDS, left),
            complete=complete,
            json_mode=False,
        )
    except JudgeFailure as failure:
        failure.cost_eur += spent
        raise
    verdict.cost_eur += spent
    verdict.seconds = time.monotonic() - started
    verdict.screen = outcome
    return verdict


# ------------------------------------------------------------------ the repair


def targets(body: str, findings: list[Finding]) -> list[tuple[int, int, str]]:
    """The sentences to rewrite, in order, each with what the check found."""
    merged: dict[tuple[int, int], list[str]] = {}
    for finding in findings:
        if finding.start < 0:
            continue
        merged.setdefault((finding.start, finding.end), []).append(
            finding.reason or "not supported by the source"
        )
    return [
        (start, min(end, len(body)), "; ".join(dict.fromkeys(reasons)))
        for (start, end), reasons in sorted(merged.items())
        if start < len(body)
    ]


def apply(body: str, spans: list[tuple[int, int, str]], edits: dict[str, Any]) -> str:
    """The answer with each numbered sentence replaced by its edit."""
    revised = body
    for number, (start, end, _) in reversed(list(enumerate(spans, start=1))):
        new = edits.get(str(number))
        if not isinstance(new, str):
            continue
        original = body[start:end]
        lead = original[: len(original) - len(original.lstrip())]
        trail = original[len(original.rstrip()) :]
        new = new.strip()
        if not new:
            line_start = body.rfind("\n", 0, start) + 1
            whole_line = not body[line_start:start].strip() and (
                end == len(body) or body[end - 1] == "\n"
            )
            if whole_line:
                revised = revised[:line_start] + revised[end:]
            else:
                revised = revised[:start] + revised[end:].lstrip(" ")
            continue
        revised = revised[:start] + lead + new + trail + revised[end:]
    return revised


def acceptable(original: str, revised: str) -> str:
    """Empty when a rewrite is kept, else why it is not."""
    if not revised.strip():
        return "empty"
    if len(revised) < 0.3 * len(original) or len(revised) > 1.3 * len(original) + 200:
        return "length"
    if not set(_LINK.findall(revised)) <= set(_LINK.findall(original)):
        return "links"
    return ""


async def rewrite(
    body: str,
    findings: list[Finding],
    source: Source,
    profile: str,
    seconds: float,
    report: Report,
    complete: Complete | None = None,
) -> str | None:
    """One call rewriting the flagged sentences only; None when not usable."""
    spans = targets(body, findings)
    if not spans:
        report.rewrite = "skipped:unlocated"
        return None
    numbered = "\n".join(
        f"[{number}] {body[start:end].strip()}\n    check: {reason}"
        for number, (start, end, reason) in enumerate(spans, start=1)
    )
    messages = [
        {"role": "system", "content": REWRITE},
        {
            "role": "user",
            "content": f"SENTENCES:\n{numbered}\n\nSOURCE:\n{source.text}",
        },
    ]
    payload = _payload(messages, profile, REWRITE_TOKENS, seconds, REWRITE_BUDGET_EUR)
    report.calls += 1
    try:
        reply = await (complete or _complete)(payload, seconds + 5)
    except Exception as error:  # noqa: BLE001 - the answer is still delivered
        report.rewrite = "failed:" + _failure_code(error)
        return None
    if isinstance(reply, dict):
        report.cost_eur += _cost(reply)
    text = reply.get("text") if isinstance(reply, dict) else None
    from packages.model_json import object_content

    try:
        raw = str(text or "").strip()
        opening, closing = raw.find("{"), raw.rfind("}")
        edits = object_content(raw[opening : closing + 1] if opening >= 0 else raw)
    except ValueError:
        report.rewrite = "failed:unreadable"
        return None
    revised = apply(body, spans, edits)
    reason = acceptable(body, revised)
    if reason:
        report.rewrite = "rejected:" + reason
        return None
    report.rewrite = "kept"
    return revised


def note(items: list[str], language: str, *, judged: bool = True) -> str:
    """The honest note naming what is still unsupported, in the answer language."""
    template = (NOTE if judged else DETAILS_NOTE).get(language) or (
        NOTE if judged else DETAILS_NOTE
    )["en"]
    quoted = "; ".join(
        "« " + item + " »" if language == "fr" else "“" + item + "”"
        for item in dict.fromkeys(i.strip() for i in items if i.strip())
    )
    return "*" + template.format(items=quoted) + "*"


def structured(body: str) -> bool:
    """True when the answer is one JSON object or array, fenced or not."""
    text = body.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0] if text.rstrip().endswith("```") else text
        text = text.strip()
    if not text.startswith(("{", "[")):
        return False
    try:
        return isinstance(json.loads(text), (dict, list))
    except ValueError:
        return False


def joined(answer: str, addition: str) -> str:
    """The answer followed by its note, as streamed (a blank line between)."""
    return answer + "\n\n" + addition if addition else answer


def statement(body: str, finding: Finding) -> str:
    text = " ".join(body[finding.start : finding.end].split()).lstrip("-*+> ")
    text = text.replace("**", "")
    if len(text) > NOTE_ITEM_CHARACTERS:
        text = text[: NOTE_ITEM_CHARACTERS - 1].rstrip() + "…"
    return text


# ------------------------------------------------------------------ the check


def _left(deadline: float | None) -> float:
    if deadline is None:
        return 2 * (JUDGE_SECONDS + SCREEN_SECONDS) + REWRITE_SECONDS
    return deadline - time.monotonic() - MARGIN_SECONDS


async def review(
    answer: str,
    source: Source,
    *,
    request: str,
    profile: str | None,
    language: str,
    streamed: bool,
    seconds: float | None = None,
    setting: str | None = None,
    complete: Complete | None = None,
) -> tuple[str, str, Report]:
    """Check an answer derived from a provided source; never raises.

    Returns (answer, addition, report): the answer to store and send when it
    was not streamed yet (rewritten when kept), and the text to append after
    it (a note, empty when none). seconds is the time the turn can spare.
    """
    started = time.monotonic()
    report = Report(mode=setting or mode(), source_characters=len(source.text))
    deadline = started + seconds if seconds is not None else None
    body, separator, footer = answer.partition("<details>")
    body = body.rstrip()

    def done(
        outcome: str, text: str = answer, addition: str = ""
    ) -> tuple[str, str, Report]:
        report.outcome = report.outcome or outcome
        report.seconds = time.monotonic() - started
        LOGGER.warning("source_faithfulness %s", json.dumps(report.metadata()))
        return text, addition, report

    if report.mode == "off":
        return answer, "", report
    if len(body.split()) < MIN_WORDS and not _NO_SPACES.search(body):
        return done("skipped:short")
    if structured(body):
        # A machine-readable answer (JSON asked by the person or a client): a
        # note or a rewritten sentence would break what the reader parses.
        return done("skipped:structured")
    found = hints(body, source)
    report.hints = len(found)
    judge_profile, rewrite_profile = profiles(profile)
    if len(source.text) > SOURCE_LIMIT:
        report.outcome = "skipped:source_too_long"
        if found and report.mode == "on":
            return done("", answer, note(found, language, judged=False))
        return done("")
    if judge_profile is None or _left(deadline) < MIN_SECONDS:
        report.outcome = "skipped:" + (
            "no_profile" if judge_profile is None else "deadline"
        )
        return done("")
    screen = screen_profile(profile)
    report.calls += 1
    try:
        verdict = await checked(
            request,
            source,
            body,
            judge_profile,
            screen,
            min(check_seconds(screen), _left(deadline)),
            complete=complete,
        )
    except JudgeFailure as failure:
        # D5: logged with its code; the answer is delivered and claims nothing.
        report.cost_eur += failure.cost_eur
        report.outcome = "failed:" + failure.code
        if found and report.mode == "on":
            return done("", answer, note(found, language, judged=False))
        return done("")
    report.cost_eur += verdict.cost_eur
    report.judge_seconds.append(verdict.seconds)
    report.screen.append(verdict.screen)
    report.links = verdict.links
    report.unlocated = verdict.unlocated
    report.not_confirmed = verdict.not_confirmed
    report.findings = report.findings_after = len(verdict.findings)
    if not verdict.findings:
        return done("clean")
    if report.mode == "log":
        return done("logged")
    remaining = verdict.findings
    text = body
    if (
        not streamed
        and rewrite_profile is not None
        and _left(deadline) >= REWRITE_SECONDS + RECHECK_SECONDS
    ):
        revised = await rewrite(
            body,
            verdict.findings,
            source,
            rewrite_profile,
            min(REWRITE_SECONDS, _left(deadline) - RECHECK_SECONDS),
            report,
            complete,
        )
        if revised is not None:
            # The rewrite is checked like the answer; unchecked, it is not kept.
            report.calls += 1
            try:
                after = await checked(
                    request,
                    source,
                    revised,
                    judge_profile,
                    screen,
                    min(check_seconds(screen), _left(deadline)),
                    complete=complete,
                )
            except JudgeFailure as failure:
                report.cost_eur += failure.cost_eur
                report.rewrite = "unchecked:" + failure.code
            else:
                report.cost_eur += after.cost_eur
                report.judge_seconds.append(after.seconds)
                report.screen.append(after.screen)
                text, remaining = revised, after.findings
                report.findings_after = len(remaining)
    elif not streamed:
        report.rewrite = "skipped:" + (
            "no_profile" if rewrite_profile is None else "deadline"
        )
    final = text + ("\n\n" + separator + footer if separator else "")
    if not remaining:
        return done("revised", final)
    addition = note([statement(text, f) for f in remaining], language)
    outcome = "revised_noted" if text != body else "noted"
    return done(outcome, final, addition)
