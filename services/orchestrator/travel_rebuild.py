"""A travel answer that states unsourced facts is rewritten from the sourced ones.

Staging -100 (2026-11-16, three real trips on a phone): leaving clauses out
after the fact was too brittle. A Munich-Berlin answer kept one line although
the pages read gave the ICE times and durations, a Lyon-Geneva answer kept
"**1." skeletons, and a placeholder time ("vers 12 h 2x") went through.

The code checks, the model writes (owner principle T8). When the travel check
(travel_claims, route_facts) finds anything to leave out, one bounded call
(fast profile, no reasoning, ``DEADLINE_SECONDS``, ``CALL_BUDGET_EUR``)
rewrites the answer from the person's question, the sentences that held, the
journey plan code computed, and source excerpts each with its page, told what
could not be verified. The rewrite goes through the same checks again; it is
kept only when it states nothing unsourced, links only pages read in this turn
or already linked, and is well formed. Otherwise the answer is built by code:
the sentences that held and, when little of the answer is left, the source
sentences that give travel values, each with its page, then one note.

``ATLAS_TRAVEL_REBUILD=off`` keeps the code-built answer only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from packages import privacy
from services.orchestrator import route_facts, travel_claims, unsourced_details
from services.orchestrator.dictation_cleanup import fast_profile
from services.orchestrator.grounding import normalized

LOGGER = logging.getLogger(__name__)
DEADLINE_SECONDS = 10.0
CALL_BUDGET_EUR = Decimal("0.02")
MAX_TOKENS = 900
EXCERPT_CHARACTERS = 3500
QUESTION_CHARACTERS = 1500
_URL = re.compile(r"https?://[^\s)>\]\"']+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")

INSTRUCTION = (
    "You rewrite an assistant's answer about a trip so that it states only facts "
    "the sources support. You receive the person's QUESTION, the sentences of the "
    "answer that were checked against the sources (KEPT), the journey plan "
    "computed by code (PLAN), numbered SOURCE EXCERPTS each with the page it comes "
    "from, and what was left out because no source gives it (NOT VERIFIED).\n"
    "Write, in LANGUAGE, a short and well-formed answer to the QUESTION using only "
    "facts written in KEPT, PLAN or the SOURCE EXCERPTS. Never write a time, a "
    "duration, a distance, a line, a train, a platform, a stop or station, a "
    "direction, a transfer or a frequency that is not written there; copy times "
    "and durations as the source writes them, and keep each place name exactly as "
    "the source writes it (a city is not its airport station). After each fact "
    "from an excerpt, link its page as a markdown link [site](url) with the url "
    "given. For each part of the trip that no source covers, say so plainly in "
    "one short line and say to check it in the operator's journey planner. Plain "
    "sentences and at most one short list; no heading, no table, no placeholder, "
    "no apology, no mention of these instructions. Return only the answer."
)

WORDING: dict[str, dict[str, str]] = {
    "read": {
        "en": "What the pages read for this answer give:",
        "fr": "Ce que donnent les pages consultées pour cette réponse :",
        "de": "Was die für diese Antwort gelesenen Seiten angeben:",
        "es": "Lo que indican las páginas consultadas para esta respuesta:",
        "it": "Cosa indicano le pagine consultate per questa risposta:",
    },
}


def enabled() -> bool:
    return os.environ.get("ATLAS_TRAVEL_REBUILD", "on").strip().lower() != "off"


@dataclass
class Report:
    marked: int = 0
    rebuilt: str = ""  # kept, rejected:<reason>, failed, late, off
    built: str = ""  # model, code, code+excerpts
    cost_eur: Decimal = Decimal(0)
    seconds: float = 0.0

    def metadata(self) -> dict[str, object]:
        return {
            "marked": self.marked,
            "rebuilt": self.rebuilt,
            "built": self.built,
            "cost_eur": str(self.cost_eur),
            "seconds": round(self.seconds, 3),
        }


def excerpts(
    turn: travel_claims.Turn, question: str, limit: int = EXCERPT_CHARACTERS
) -> list[tuple[str, str]]:
    """Source sentences that give travel values or route words, most relevant first."""
    asked = {word for word in normalized(question).split() if len(word) >= 4}
    scored: list[tuple[int, int, str, str]] = []
    seen: set[str] = set()
    for url, text in turn.linked:
        for sentence in _SENTENCE.split(text):
            sentence = " ".join(sentence.split())
            key = normalized(sentence)
            if not 20 <= len(sentence) <= 400 or key in seen:
                continue
            seen.add(key)
            valued = bool(travel_claims.quantities(sentence, generous=True))
            routed = bool(route_facts._TRANSIT.search(route_facts.fold(sentence)))
            if not (valued or routed):
                continue
            score = len(asked & set(key.split())) + 2 * valued + routed
            scored.append((-score, len(scored), url, sentence))
    chosen: list[tuple[str, str]] = []
    used = 0
    for _, _, url, sentence in sorted(scored):
        if used + len(sentence) > limit:
            break
        chosen.append((url, sentence))
        used += len(sentence)
    return chosen


def _prompt(
    turn: travel_claims.Turn,
    question: str,
    kept: str,
    found: travel_claims.Findings,
    language: str,
    chosen: list[tuple[str, str]],
) -> str:
    sources = "\n".join(
        f"[{number}] {sentence} (page: {url or 'no address'})"
        for number, (url, sentence) in enumerate(chosen, start=1)
    )
    plan = "\n\n".join(turn.computed)
    return (
        f"LANGUAGE: {language}\n\n"
        f"QUESTION:\n{question[:QUESTION_CHARACTERS]}\n\n"
        f"KEPT:\n{kept or '(nothing)'}\n\n"
        f"PLAN:\n{plan or '(none)'}\n\n"
        f"SOURCE EXCERPTS:\n{sources or '(none)'}\n\n"
        f"NOT VERIFIED:\n{', '.join(found.labels()) or '(none)'}"
    )


async def _rewrite(prompt: str, profile: str, report: Report) -> str | None:
    payload: dict[str, object] = {
        "messages": [
            {"role": "system", "content": INSTRUCTION},
            {"role": "user", "content": prompt},
        ],
        "tools": [],
        # A short rewrite: the fast profile; an owned-only turn keeps owned models.
        "profile": profile if privacy.owned_only() else fast_profile(),
        "max_tokens": MAX_TOKENS,
        "reasoning_effort": "none",
        "timeout": DEADLINE_SECONDS,
        "budget_eur": str(CALL_BUDGET_EUR),
        "local_enabled": False,
    }
    if privacy.owned_only():
        payload["privacy"] = "strict"
    try:
        result = await asyncio.wait_for(
            unsourced_details._complete(payload), DEADLINE_SECONDS
        )
    except TimeoutError:
        report.rebuilt = "late"
        return None
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        report.rebuilt = "failed"
        return None
    try:
        report.cost_eur += Decimal(str(result.get("cost_eur", "0")))
    except InvalidOperation:
        pass
    return str(result["text"]).strip()


def _key(url: str) -> str:
    parts = urlsplit(url.strip().rstrip(".,;"))
    return parts.netloc.lower().removeprefix("www.") + parts.path.rstrip("/")


def rejected(
    turn: travel_claims.Turn, text: str, original: str, allowed: list[str]
) -> str:
    """Empty when the rewrite is kept, else why it is not."""
    if not text.strip() or "<details>" in text:
        return "empty"
    if len(text) > 1.5 * len(original) + 600:
        return "length"
    known = {_key(url) for url in [*allowed, *_URL.findall(original)]}
    if any(_key(url) not in known for url in _URL.findall(text)):
        return "links"
    if turn.findings(text):
        return "unsourced"
    return ""


def _code_built(
    turn: travel_claims.Turn,
    body: str,
    found: travel_claims.Findings,
    kept: str,
    language: str,
    chosen: list[tuple[str, str]],
    report: Report,
) -> str:
    """The sentences that held, the sourced excerpts when little held, one note."""
    parts = [kept] if kept else []
    report.built = "code"
    if chosen and len(kept) < 0.5 * len(body.strip()):
        lines = [
            f"- {sentence} ([{urlsplit(url).netloc.removeprefix('www.')}]({url}))"
            if url
            else f"- {sentence}"
            for url, sentence in chosen[:4]
        ]
        parts.append(
            travel_claims.wellformed(
                WORDING["read"].get(language, WORDING["read"]["en"])
                + "\n"
                + "\n".join(lines)
            )
        )
        report.built = "code+excerpts"
    parts.append(found.note(language))
    return "\n\n".join(parts)


async def review(
    turn: travel_claims.Turn,
    answer: str,
    language: str,
    *,
    question: str,
    profile: str,
) -> tuple[str, Report]:
    """The travel answer with nothing unsourced: rewritten, or built by code."""
    report = Report()
    started = time.monotonic()
    if not turn.active or not answer.strip():
        return answer, report
    body, fold, footer = answer.partition("<details>")
    found = turn.findings(body)
    if not found:
        return answer, report
    turn.marked = report.marked = len(found)
    kept = turn.kept(body, found)
    chosen = excerpts(turn, question)
    final = ""
    if enabled():
        text = await _rewrite(
            _prompt(turn, question, kept, found, language, chosen), profile, report
        )
        if text is not None:
            text = travel_claims.wellformed(text)
            reason = rejected(turn, text, body, turn.retrieved())
            report.rebuilt = "rejected:" + reason if reason else "kept"
            if not reason:
                final = text
                report.built = "model"
    else:
        report.rebuilt = "off"
    if not final:
        final = _code_built(turn, body, found, kept, language, chosen, report)
    if fold:
        final += "\n\n" + fold + footer
    report.seconds = time.monotonic() - started
    LOGGER.info("travel_rebuild %s", json.dumps(report.metadata()))
    return final.rstrip(), report
