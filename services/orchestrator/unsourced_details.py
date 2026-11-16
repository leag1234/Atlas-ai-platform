"""Precise details an answer adds from memory around sourced facts.

The dominant measured quality defect is an answer that states a sourced fact and
then adds precise details nobody retrieved: founding members, a full birth date,
a height, the origin of a name. The code finds them, the model decides what to do
with them (owner principle "the code does and checks, the model decides"):

1. every precise detail of the answer is extracted (a full date, a year, a number
   with its scale word, a proper name, a quoted title);
2. each one is looked up in the texts the turn retrieved (search results, fetched
   pages, specialized sources such as the encyclopedia facts), in the person's own
   messages, or as the same value in another written form (another language's
   month name, an ISO date, another thousands separator, a rounded number);
3. the details no retrieved text holds go to one bounded revision call: the model
   removes each one, or replaces it with what the given source excerpts state, and
   changes nothing else;
4. the revision is checked by code and kept only when it removes unsupported
   details without adding any new one, keeps every link, and stays an answer;
   otherwise the original answer is kept, and in ``revise_mark`` mode the details
   still unsupported are marked next to their sentence, in the answer language.

Before the revision, the details no retrieved text holds are looked up in the
subject's full encyclopedia articles (the pages the turn reached, the article the
question names, its counterpart in English or the answer's language): a detail
found there is sourced, and the article joins the consulted sources. Only standard
and enhanced levels read the live encyclopedia here.

``ATLAS_UNSOURCED_DETAILS``: ``off`` (default), ``mark``, ``revise``,
``revise_mark``. It applies to a held factual turn (grounding's cue, or a
question about a named subject when the setting is on), with or without
retrieved sources; grounding runs after it on the final text. An answer whose
details are mostly unsupported (the sources do not cover it) is left as it is.
Every pass over the answer and the sources is linear in their size.
"""

from __future__ import annotations

import asyncio
from bisect import bisect_left
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
import logging
import os
import re
import time

from urllib.parse import unquote

import aiohttp

from packages import privacy
from packages.ports import atlas_ports
from services.orchestrator import encyclopedia, reference_fetch
from services.orchestrator.dictation_cleanup import fast_profile
from services.orchestrator.grounding import STOP as STOP_WORDS
from services.orchestrator.grounding import (
    CREATIVE,
    needs_grounding,
    normalized,
    question_entities,
    small_talk,
    terms,
)

LOGGER = logging.getLogger(__name__)
MODES = frozenset({"off", "mark", "revise", "revise_mark"})
# One revision call: its ceiling and its time (the answer is held meanwhile).
CALL_BUDGET_EUR = Decimal("0.02")
DEADLINE_SECONDS = float(os.environ.get("ATLAS_UNSOURCED_DETAILS_SECONDS", "12"))
EXCERPT_CHARACTERS = 4000
# The reference check: full encyclopedia articles of the subject, read once.
REFERENCE_SECONDS = 6.0
REFERENCE_ARTICLES = 4
WIKI_URL = re.compile(r"https://([a-z]{2,3})\.wikipedia\.org/wiki/([^\s\"'<>#?|\]]++)")
MAX_DETAILS = 40
# Beyond this share of unsupported details the sources do not cover the answer (a
# broad answer from memory): stripping or flagging most of it would leave no answer,
# so it is left as it is and counted.
UNCOVERED_SHARE = 0.4

MONTHS: dict[str, int] = {}
for _number, _names in enumerate(
    (
        "january janvier januar enero gennaio jan",
        "february fevrier februar febrero febbraio feb fev",
        "march mars marz marzo",
        "april avril abril aprile apr avr",
        "may mai mayo maggio",
        "june juin juni junio giugno jun",
        "july juillet juli julio luglio jul juil",
        "august aout agosto aug",
        "september septembre septiembre settembre sep sept",
        "october octobre oktober octubre ottobre oct okt",
        "november novembre noviembre nov",
        "december decembre dezember diciembre dicembre dec dez",
    ),
    start=1,
):
    for _name in _names.split():
        MONTHS[_name] = _number
NUMBER_WORDS: dict[int, frozenset[str]] = {
    value: frozenset(words.split())
    for value, words in enumerate(
        (
            "zero zero null cero",
            "one un une eins uno una",
            "two deux zwei dos due",
            "three trois drei tres tre",
            "four quatre vier cuatro quattro",
            "five cinq funf cinco cinque",
            "six six sechs seis sei",
            "seven sept sieben siete sette",
            "eight huit acht ocho otto",
            "nine neuf neun nueve nove",
            "ten dix zehn diez dieci",
            "eleven onze elf once undici",
            "twelve douze zwolf doce dodici",
        )
    )
}
SCALES = {
    "thousand": 1e3,
    "mille": 1e3,
    "tausend": 1e3,
    "mil": 1e3,
    "million": 1e6,
    "millions": 1e6,
    "millionen": 1e6,
    "millones": 1e6,
    "milioni": 1e6,
    "milione": 1e6,
    "millon": 1e6,
    "billion": 1e9,
    "billions": 1e9,
    "milliard": 1e9,
    "milliards": 1e9,
    "milliarden": 1e9,
    "miliardi": 1e9,
    "miliardo": 1e9,
    "milliarde": 1e9,
}
# Words written with a capital at the start of a sentence that are not names.
COMMON = frozenset(
    (
        "a an the this that these those it he she they we you i there here "
        "however also but so then today yes no note source sources summary "
        "le la les l un une des du de d ce cet cette ces il elle ils elles on nous "
        "vous je mais donc puis ensuite enfin aujourd hui oui non voici selon "
        "parmi apres avant depuis en au aux pour par avec sur dans "
        "der die das ein eine es er sie wir und aber el los las una y pero "
        "il lo gli e ma wikipedia wikidata"
    ).split()
) | frozenset(MONTHS)
# Words that join the parts of one name ("Charles de Gaulle", "Bank of England").
CONNECTORS = frozenset(
    "de du des la le les d l von van der den of the di da del dos".split()
)
STOP = CONNECTORS | frozenset("a an in on at to for by with and et y und e".split())
# Titles before a name ("King Charles VIII", "President Ilse Varnholt"): a source
# naming the person without the title still holds the name.
TITLES = frozenset(
    (
        "king queen prince princess emperor empress pope saint st sir lord lady dame "
        "president general captain duke count dr mr mrs ms "
        "roi reine princesse empereur imperatrice pape sainte president presidente "
        "general generale capitaine duc duchesse comte comtesse m mme "
        "konig konigin kaiser papst rey reina emperador papa santo santa re regina "
        "imperatore"
    ).split()
)

# A full date: "18 December 1970", "December 18, 1970", "1st May 1968" (any of
# the five interface languages).
DAY_FIRST = re.compile(
    r"\b(\d{1,2})(?:er|st|nd|rd|th|\.)?\s([^\W\d_]{3,10})\.?\s(\d{4})\b"
)
MONTH_FIRST = re.compile(
    r"\b([^\W\d_]{3,10})\.?\s(\d{1,2})(?:st|nd|rd|th)?,?\s(\d{4})\b"
)
YEAR = re.compile(r"\b(?:1[0-9]{3}|20[0-9]{2})\b")
# A numeric date: "2004-12-16", "16/12/2004", "16.12.2004".
NUMERIC_DATE = re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})\b|\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b"
)
# A number: grouped thousands or plain digits, a decimal part, a scale word.
NUMBER = re.compile(
    r"(?<![\w.,])(?:\d{1,3}(?:[   .,]\d{3})++|\d++)(?:[.,]\d++)?+"
    r"(?:\s?(?:%|[^\W\d_]{4,11}))?+"
)
QUOTE = re.compile(r"[«“\"]\s?([^«»“”\"\n]{3,80})\s?[»”\"]")
WORD = re.compile(r"[^\W_]++(?:['’\-][^\W_]++)*+")
SENTENCE_END = re.compile(r"[.!?](?=\s)|\n")
# Abbreviations that a full stop does not end a sentence after ("No. 2", "St. Paul").
ABBREVIATIONS = frozenset(
    "no nos nr st ste dr mr mrs ms jr sr mt vs etc cf p pp vol art ch fig ca env "
    "mme mlle m av apr bd".split()
)
MARKUP = re.compile(r"\*\*|__|`|^\s*(?:[-*+>]|\d{1,3}[.)]|#{1,6})\s", re.M)


def mode() -> str:
    value = os.environ.get("ATLAS_UNSOURCED_DETAILS", "off").strip().lower()
    return value if value in MODES else "off"


# Questions about a named subject that grounding's precise-fact cues miss. A request
# to present someone or something ("tell me about", "who is") is enough; a generic
# "what is" or "which" counts only with a capitalised name in the question, so a
# definition or an explanation ("what is the difference between...") keeps streaming.
ABOUT = re.compile(
    r"\b(?:tell me about|talk about|who s|who is|who was|parle moi de|parle moi du|"
    r"parle moi des|presente moi|c est qui|qui est|qui etait|"
    r"erzahl mir von|wer ist|wer war|hablame de|quien es|quien era|parlami di|"
    r"chi e|chi era)\b"
)
GENERIC_ABOUT = re.compile(
    r"\b(?:what s|what is|what was|what are|what were|which|wich|whose|c est quoi|"
    r"qu est ce que|was ist|que es|cos e)\b"
)


OWN = re.compile(
    r"\b(?:my|our|mon|ma|mes|notre|nos|mein|meine|unser|mi|mis|mio|mia|miei)\b"
)


def wants_review(question: str) -> bool:
    """A question whose answer is held so its details can be checked (mode on only).

    Grounding's own cue (needs_grounding) plus questions about a person, a place,
    a company or a work asked without a precise-fact word. Decided before any
    streaming, like grounding.
    """
    if mode() == "off":
        return False
    text = normalized(question)
    if not text or CREATIVE.search(text) or small_talk(question) or len(text) > 400:
        return False
    if needs_grounding(question):
        return True
    if OWN.search(text):
        # "my server", "notre facture": the person's own things, not a public subject.
        return False
    return bool(ABOUT.search(text)) or bool(
        GENERIC_ABOUT.search(text) and question_entities(question)
    )


@dataclass(frozen=True)
class Detail:
    kind: str  # date, year, number, name, title
    text: str
    start: int  # offset of the sentence holding it in the answer body
    end: int

    @property
    def key(self) -> str:
        return self.kind + ":" + normalized(self.text)


def _sentences(body: str) -> list[tuple[int, int]]:
    spans = []
    start = 0
    for match in SENTENCE_END.finditer(body):
        if match[0] == ".":
            words = body[max(start, match.start() - 6) : match.start()].split()
            word = words[-1].strip("(\"'*_«“[").casefold() if words else ""
            following = body[match.end() : match.end() + 3].lstrip()
            if (
                (word and (word in ABBREVIATIONS or len(word) == 1))
                or not following
                or following[0].isdigit()
                or following[0].islower()
            ):
                # "No. 2", "J. Smith", "etc. and": the sentence goes on.
                continue
        if body[start : match.end()].strip():
            spans.append((start, match.end()))
        start = match.end()
    if body[start:].strip():
        spans.append((start, len(body)))
    return spans


def _number_value(text: str) -> tuple[float, float] | None:
    """The value of a written number and the precision it is written with."""
    match = re.match(r"(\d{1,3}(?:[   .,]\d{3})++|\d++)(?:([.,])(\d++))?+", text)
    if match is None:
        return None
    whole = re.sub(r"\D", "", match[1])
    decimals = match[3] or ""
    if match[2] and len(decimals) == 3 and not re.search(r"[   ]", match[1]):
        # "1,200" or "1.200" alone: thousands, not a decimal part.
        whole, decimals = whole + decimals, ""
    value = float(whole + ("." + decimals if decimals else ""))
    zeros = len(whole) - len(whole.rstrip("0")) if whole.strip("0") else 0
    # A round number ("47 000", "1,2 million") may be a rounded source value; any
    # other number (a year, "324 m") must be found exactly.
    if decimals:
        precision = 10.0 ** -len(decimals)
    else:
        precision = 10.0**zeros if zeros >= 3 else 0.0
    word = normalized(text[match.end() :]).split()
    scale = SCALES.get(word[0], 1.0) if word else 1.0
    return value * scale, precision * scale


def extract(answer: str) -> list[Detail]:
    """Precise details of an answer, the source footer and code left out."""
    body = answer.split("<details>", 1)[0]
    found: dict[str, Detail] = {}
    in_fence = False
    for start, end in _sentences(body):
        sentence = body[start:end]
        if sentence.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = sentence.strip()
        # Headings, bold labels and table rows name sections, not facts: their
        # numbers and dates are read, their capitalised words are not names.
        heading = stripped.startswith(("#", "|")) or (
            stripped.startswith("**") and stripped.rstrip(" :").endswith("**")
        )
        plain = MARKUP.sub(
            " ", re.sub(r"\]\([^)\s]*\)|https?://\S+|\[\d{1,3}\]", " ", sentence)
        )
        taken: list[tuple[int, int]] = []

        def add(kind: str, text: str, span: tuple[int, int]) -> None:
            taken.append(span)
            detail = Detail(kind, text.strip(), start, end)
            if normalized(detail.text):
                found.setdefault(detail.key, detail)

        for match in NUMERIC_DATE.finditer(plain):
            year, month, day = (
                (match[1], match[2], match[3])
                if match[1]
                else (match[6], match[5], match[4])
            )
            if 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
                add("date", f"{int(day)} {int(month)} {year}", match.span())
        for pattern, month_group in ((DAY_FIRST, 2), (MONTH_FIRST, 1)):
            for match in pattern.finditer(plain):
                if normalized(match[month_group]) in MONTHS:
                    add("date", match[0], match.span())
        for match in YEAR.finditer(plain):
            if not any(a <= match.start() < b for a, b in taken):
                add("year", match[0], match.span())
        for match in NUMBER.finditer(plain):
            if any(a <= match.start() < b for a, b in taken):
                continue
            text = match[0]
            tail = normalized(text).split()
            if len(tail) > 1 and tail[-1] not in SCALES and "%" not in text:
                text = text.split()[0]
            digits = re.sub(r"\D", "", text)
            if text == digits and len(digits) == 1:
                # A lone digit counts only before a noun ("4 founders"), not as a
                # position or a part ("1 of", "chapter 2").
                after = plain[match.end() : match.end() + 16]
                if not re.match(r"\s[^\W\d_]{3}", after):
                    continue
            add("number", text, match.span())
        for match in QUOTE.finditer(plain):
            add("title", match[1], match.span(1))
        words = list(WORD.finditer(plain))
        index = 0
        while index < len(words):
            word = words[index][0]
            if not word[0].isupper():
                index += 1
                continue
            last = index
            following = index + 1
            while following < len(words):
                gap = plain[words[following - 1].end() : words[following].start()]
                if gap not in {" ", " "}:
                    break
                candidate = words[following][0]
                if candidate[0].isupper():
                    last = following
                    following += 1
                elif (
                    normalized(candidate) in CONNECTORS
                    and following + 1 < len(words)
                    and words[following + 1][0][0].isupper()
                    and plain[words[following].end() : words[following + 1].start()]
                    in {" ", " "}
                ):
                    last = following + 1
                    following += 2
                else:
                    break
            text = plain[words[index].start() : words[last].end()]
            first = normalized(word)
            single = last == index
            sentence_start = (
                not plain[: words[index].start()]
                .rstrip(' (\t*_"«“|')[-1:]
                .strip(".!?:|")
            )
            if sentence_start and first in STOP | COMMON and not single:
                # "The Xyz", "Le Xyz": the article opening the sentence is no part of the name.
                index += 1
                continue
            # A capitalised word opening a sentence is an ordinary word unless it
            # reads as a name: a list entry ("Name (...)", "Name,"), an acronym or
            # an inner capital.
            after = plain[words[last].end() :].lstrip(" *_")
            name_like = (
                not after
                or after[0] in "(,–—-"
                or word.isupper()
                or any(c.isupper() for c in word[1:])
            )
            if not (
                first in MONTHS
                or heading
                or (single and (first in COMMON or len(word) < 3))
                or (single and sentence_start and not name_like)
            ):
                detail_start = len(taken)
                add("name", text, (words[index].start(), words[last].end()))
                del taken[detail_start:]
            index = following
        if len(found) >= MAX_DETAILS:
            break
    return list(found.values())


@dataclass
class Evidence:
    """Retrieved texts indexed once: words, phrases and number values."""

    text: str = ""
    words: frozenset[str] = frozenset()
    values: list[float] = field(default_factory=list)
    lowercase: frozenset[str] = frozenset()

    @classmethod
    def of(cls, passages: list[str]) -> Evidence:
        joined = "\n".join(passages)
        text = " " + normalized(joined) + " "
        values = sorted(
            value
            for match in NUMBER.finditer(joined)
            if (parsed := _number_value(match[0])) is not None
            for value in (parsed[0],)
        )
        lowercase = frozenset(
            normalized(match[0])
            for match in WORD.finditer(joined)
            if match[0][0].islower()
        )
        return cls(text, frozenset(text.split()), values, lowercase)

    def holds(self, phrase: str) -> bool:
        return bool(phrase) and f" {phrase} " in self.text

    def value(self, value: float, precision: float) -> bool:
        tolerance = precision / 2 + abs(value) * 1e-9
        index = bisect_left(self.values, value - tolerance)
        return index < len(self.values) and self.values[index] <= value + tolerance


def supported(detail: Detail, evidence: Evidence, own: Evidence) -> bool:
    """True when a retrieved text (or the person's own words) holds the detail."""
    phrase = normalized(detail.text)
    if evidence.holds(phrase) or own.holds(phrase):
        return True
    if detail.kind == "name":
        tokens = [
            token
            for token in phrase.split()
            if token not in STOP and token not in TITLES
        ]
        return bool(tokens) and all(
            token in evidence.words or token in own.words for token in tokens
        )
    if detail.kind == "date":
        # "1er mai 1968", "May 1st, 1968": the day without its ordinal ending.
        parts = [
            re.sub(r"^(\d{1,2})(?:er|st|nd|rd|th)$", r"\1", p) for p in phrase.split()
        ]
        day = next((p for p in parts if p.isdigit() and len(p) <= 2), "")
        month = next((MONTHS[p] for p in parts if p in MONTHS), 0)
        if not month and len(parts) == 3 and parts[1].isdigit():
            month = int(parts[1])
        year = next((p for p in parts if len(p) == 4 and p.isdigit()), "")
        if not (day and month and year):
            return False
        names = [name for name, number in MONTHS.items() if number == month]
        forms = [f"{year} {month:02d} {int(day):02d}", f"{year} {month} {int(day)}"]
        for name in names:
            forms += [
                f"{int(day)} {name} {year}",
                f"{name} {int(day)} {year}",
                f"{int(day)}er {name} {year}",
            ]
        return any(evidence.holds(form) or own.holds(form) for form in forms)
    if detail.kind in {"number", "year"}:
        parsed = _number_value(detail.text)
        if parsed is None:
            return False
        value, precision = parsed
        if "%" in detail.text:
            precision = max(precision, 0.1)
        if evidence.value(value, precision) or own.value(value, precision):
            return True
        if value == int(value) and int(value) in NUMBER_WORDS:
            return any(
                word in evidence.words or word in own.words
                for word in NUMBER_WORDS[int(value)]
            )
    return False


def common_word(
    detail: Detail, evidence: Evidence, answer_lowercase: frozenset[str]
) -> bool:
    """A capitalised single word that the texts also write in lower case (an ordinary word)."""
    phrase = normalized(detail.text)
    return (
        detail.kind == "name"
        and " " not in phrase
        and (phrase in evidence.lowercase or phrase in answer_lowercase)
    )


def unsupported(answer: str, evidence: Evidence, own: Evidence) -> list[Detail]:
    body = answer.split("<details>", 1)[0]
    lowercase = frozenset(
        normalized(match[0]) for match in WORD.finditer(body) if match[0][0].islower()
    )
    details = [d for d in extract(answer) if not common_word(d, evidence, lowercase)]
    missing = [d for d in details if not supported(d, evidence, own)]
    # An age or a duration computed from two supported years ("died at 83",
    # "for 14 years") is supported by them.
    years = sorted(
        {
            int(year)
            for d in details
            if d not in missing and d.kind in {"year", "date"}
            for year in YEAR.findall(d.text)
        }
    )
    spans = {b - a for i, a in enumerate(years) for b in years[i + 1 :]}
    return [
        d
        for d in missing
        if not (
            d.kind == "number"
            and d.text.isdigit()
            and (int(d.text) in spans or int(d.text) + 1 in spans)
        )
    ]


def excerpts(answer: str, details: list[Detail], passages: list[str]) -> str:
    """Source sentences closest to the sentences holding unsupported details."""
    wanted: list[set[str]] = []
    seen_spans = set()
    for detail in details:
        if (detail.start, detail.end) not in seen_spans:
            seen_spans.add((detail.start, detail.end))
            wanted.append(terms(answer[detail.start : detail.end]))
    best: dict[int, tuple[int, str]] = {}
    for passage in passages:
        for start, end in _sentences(passage):
            sentence = passage[start:end].strip()
            if not 20 <= len(sentence) <= 600:
                continue
            words = terms(sentence)
            for index, target in enumerate(wanted):
                score = len(words & target)
                if score >= 2 and score > best.get(index, (0, ""))[0]:
                    best[index] = (score, sentence)
    chosen = list(dict.fromkeys(sentence for _, sentence in best.values()))
    text = ""
    for sentence in chosen:
        if len(text) + len(sentence) > EXCERPT_CHARACTERS:
            break
        text += "- " + sentence + "\n"
    return text


INSTRUCTION = (
    "You correct sentences of an assistant's answer so that they state only precise "
    "details that the sources retrieved in this conversation support. You receive "
    "numbered sentences of the answer, for each one the precise details (names, "
    "dates, numbers, titles) that no retrieved source contains, and source excerpts.\n"
    "For each listed detail: if an excerpt states the correct value, use the "
    "excerpt's value; otherwise remove the detail, or the part of the sentence that "
    "only exists to state it, so the sentence stays correct, natural and in its "
    "language. Never add a name, date, number or title that is not already in the "
    "sentence or in the excerpts. Keep the sentence's markdown (list marker, bold, "
    "links, citation markers) and every other word. If nothing correct remains, "
    "return an empty string for it.\n"
    "Return only a JSON object mapping each sentence number to its corrected text, "
    'for example {"1": "corrected sentence", "2": ""}.'
)


@dataclass
class Report:
    mode: str = "off"
    details: int = 0
    unsupported: int = 0
    unsupported_after: int = 0
    revised: str = ""  # kept, rejected:<reason>, failed, late, skipped:<reason>
    marked: int = 0
    cost_eur: Decimal = Decimal(0)
    seconds: float = 0.0
    removed: list[str] = field(default_factory=list)
    referenced: int = 0  # details found in the encyclopedia articles read for the check
    reference_urls: list[str] = field(default_factory=list)
    reference_texts: list[str] = field(default_factory=list, repr=False)

    def metadata(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "details": self.details,
            "unsupported": self.unsupported,
            "unsupported_after": self.unsupported_after,
            "revised": self.revised,
            "marked": self.marked,
            "referenced": self.referenced,
            "reference_articles": len(self.reference_urls),
            "cost_eur": str(self.cost_eur),
            "seconds": round(self.seconds, 3),
        }


async def _complete(payload: dict[str, object]) -> object:
    """One completion through the model gateway; None when it did not answer."""
    url = os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway"))
    try:
        async with aiohttp.ClientSession(
            trust_env=False,
            timeout=aiohttp.ClientTimeout(total=DEADLINE_SECONDS + 5),
        ) as session:
            async with session.post(
                url.rstrip("/") + "/agent/complete",
                json=payload,
                allow_redirects=False,
                headers=privacy.headers(),
            ) as response:
                if response.status != 200:
                    return None
                return await response.json(content_type=None)
    except TimeoutError:
        raise
    except (OSError, aiohttp.ClientError, ValueError):
        return None


def _targets(details: list[Detail]) -> list[tuple[int, int, list[str]]]:
    spans: dict[tuple[int, int], list[str]] = {}
    for detail in details:
        spans.setdefault((detail.start, detail.end), []).append(detail.text)
    return [(start, end, texts) for (start, end), texts in sorted(spans.items())]


async def _revise(
    body: str,
    details: list[Detail],
    passages: list[str],
    profile: str,
    language: str,
    report: Report,
) -> str | None:
    """One call that corrects only the sentences holding unsupported details."""
    targets = [
        (start, min(end, len(body)), texts)
        for start, end, texts in _targets(details)
        if start < len(body)
    ]
    numbered = "\n".join(
        f"[{number}] {body[start:end].strip()}\n    unsupported: " + "; ".join(texts)
        for number, (start, end, texts) in enumerate(targets, start=1)
    )
    content = (
        f"LANGUAGE: {language}\n\nSENTENCES:\n{numbered}\n\n"
        f"SOURCE EXCERPTS:\n{excerpts(body, details, passages) or '(none)'}"
    )
    payload: dict[str, object] = {
        "messages": [
            {"role": "system", "content": INSTRUCTION},
            {"role": "user", "content": content},
        ],
        "tools": [],
        # A short edit: the fast profile (measured 0.4 to 4 s, against 10 to 30 s
        # for the large chat profiles); an owned-only turn keeps its own profile.
        "profile": profile if privacy.owned_only() else fast_profile(),
        "max_tokens": min(4000, 200 + sum(end - start for start, end, _ in targets)),
        "timeout": DEADLINE_SECONDS,
        "budget_eur": str(CALL_BUDGET_EUR),
        "local_enabled": False,
    }
    if privacy.owned_only():
        payload["privacy"] = "strict"
    try:
        result = await _complete(payload)
    except TimeoutError:
        report.revised = "late"
        return None
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        report.revised = "failed"
        return None
    try:
        report.cost_eur += Decimal(str(result.get("cost_eur", "0")))
    except InvalidOperation:
        pass
    text = str(result["text"]).strip()
    opening, closing = text.find("{"), text.rfind("}")
    try:
        edits = json.loads(text[opening : closing + 1]) if opening >= 0 else None
    except ValueError:
        edits = None
    if not isinstance(edits, dict):
        report.revised = "failed:format"
        return None
    revised = body
    for number, (start, end, _) in reversed(list(enumerate(targets, start=1))):
        new = edits.get(str(number))
        if not isinstance(new, str):
            continue
        original = body[start:end]
        lead = original[: len(original) - len(original.lstrip())]
        trail = original[len(original.rstrip()) :]
        new = new.strip()
        if not new:
            # The whole sentence goes; a line it filled goes with its line break.
            line_start = body.rfind("\n", 0, start) + 1
            whole_line = not body[line_start:start].strip() and (
                end == len(body) or body[end - 1] == "\n"
            )
            if whole_line:
                revised = revised[:line_start] + revised[end:]
                continue
            revised = revised[:start] + revised[end:].lstrip(" ")
            continue
        revised = revised[:start] + lead + new + trail + revised[end:]
    return revised


LINK = re.compile(r"https?://[^\s)>\]]++|\]\([^)\s]++\)")


def acceptable(
    original: str, revised: str, before: list[Detail], after: list[Detail]
) -> str:
    """Empty when the revision is kept, else the reason it is not."""
    if len(revised) < 0.35 * len(original) or len(revised) > 1.3 * len(original) + 200:
        return "length"
    if not set(LINK.findall(revised)) <= set(LINK.findall(original)):
        return "links"
    original_keys = {detail.key for detail in before}
    if any(detail.key not in original_keys for detail in after):
        return "new_details"
    if len(after) >= len(before):
        return "unchanged"
    return ""


NOTE = {
    "en": "not found in the sources consulted: {details}",
    "fr": "non trouvé dans les sources consultées : {details}",
    "de": "in den abgerufenen Quellen nicht gefunden: {details}",
    "es": "no encontrado en las fuentes consultadas: {details}",
    "it": "non trovato nelle fonti consultate: {details}",
}


def mark(answer: str, details: list[Detail], language: str) -> tuple[str, int]:
    """A short note at the end of each sentence holding unsupported details."""
    body, footer = (answer.split("<details>", 1) + [""])[:2]
    by_sentence: dict[tuple[int, int], list[str]] = {}
    for detail in details:
        by_sentence.setdefault((detail.start, detail.end), []).append(detail.text)
    template = NOTE.get(language, NOTE["en"])
    for (start, end), texts in sorted(by_sentence.items(), reverse=True):
        stop = end
        while stop > start and body[stop - 1] in "\n ":
            stop -= 1
        note = " *(" + template.format(details=", ".join(dict.fromkeys(texts))) + ")*"
        body = body[:stop] + note + body[stop:]
    return body + ("<details>" + footer if footer else ""), len(by_sentence)


def _wiki_pages(
    urls: list[str], passages: list[str], language: str
) -> list[tuple[str, str]]:
    """Encyclopedia articles the turn already reached, the answer's edition first."""
    pages: dict[tuple[str, str], None] = {}
    for text in [*urls, *passages]:
        for match in WIKI_URL.finditer(text):
            title = unquote(match[2]).replace("_", " ")
            while title.endswith(")") and title.count(")") > title.count("("):
                title = title[:-1]  # a link closed by the markdown around it
            if ":" not in title:
                pages.setdefault((match[1], title))
    return sorted(pages, key=lambda page: page[0] != language)


@dataclass(frozen=True)
class Article:
    url: str
    text: str
    # The same subject in another edition: (edition, title), when listed.
    other: tuple[str, str] | None = None


async def _article(
    edition: str, title: str, timeout: float, other_edition: str
) -> Article | None:
    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "prop": "extracts|info|langlinks",
        "explaintext": "1",
        "redirects": "1",
        "inprop": "url",
        "lllang": other_edition,
        "titles": title,
    }
    body = await reference_fetch.get(
        f"https://{edition}.wikipedia.org/w/api.php",
        capability=encyclopedia.CAPABILITY,
        timeout=timeout,
        ttl=encyclopedia.CACHE_SECONDS,
        max_bytes=4_000_000,
        params=params,
    )
    try:
        pages = json.loads(body).get("query", {}).get("pages", [])
    except (ValueError, AttributeError):
        return None
    page = pages[0] if pages and isinstance(pages[0], dict) else {}
    text = page.get("extract")
    url = page.get("canonicalurl") or page.get("fullurl")
    if not isinstance(text, str) or not text.strip() or not isinstance(url, str):
        return None
    links = page.get("langlinks") or []
    other = None
    if links and isinstance(links[0], dict) and links[0].get("title"):
        other = (other_edition, str(links[0]["title"]))
    return Article(url, text, other)


# Question and filler words left out of the encyclopedia search for the subject.
ASK_WORDS = frozenset(
    (
        "who whom whose when where which wich what whats how many much why did does do "
        "done tell me about talk please can could you is are was were been be "
        "qui quoi quand quel quelle quels quelles comment combien pourquoi ou c est ce cela ca "
        "qu moi parle dis peux tu vous ete etait sont avait fait moment "
        "wer wann wo welche welcher wie warum ist war quien cuando donde cual como "
        "chi quando dove quale come perche"
    ).split()
)


def subject_query(question: str) -> str:
    """The question's words without question words and stop words ("first paris marathon held")."""
    words = [
        word
        for word in normalized(question).split()
        if len(word) > 1 and word not in STOP_WORDS and word not in ASK_WORDS
    ]
    return " ".join(words)[:200] or " ".join(question_entities(question))


async def references(
    question: str, language: str, urls: list[str], passages: list[str]
) -> list[tuple[str, str]]:
    """Full articles of the subject from the live encyclopedia, when the level allows it.

    The articles the turn reached, the article found for the question's own words,
    and the first one's counterpart in the other edition (English, or the answer's
    language), at most REFERENCE_ARTICLES. Strict turns (and the local-only setup)
    read no live source here: the check then uses what the turn retrieved.
    """
    if encyclopedia.source_plan() not in {"live", "live_then_local"}:
        return []
    edition = encyclopedia.wiki_language(language)

    async def one(wiki: str, title: str) -> Article | None:
        other = "en" if wiki != "en" else (edition if edition != "en" else "fr")
        try:
            return await _article(wiki, title, REFERENCE_SECONDS, other)
        except reference_fetch.SourceError as exc:
            LOGGER.info(
                "unsourced_details_reference %s", json.dumps({"error": exc.code})
            )
            return None

    async def searched() -> list[tuple[str, str]]:
        # The question's words, then the names it gives: "signe traite maastricht
        # france" finds the treaty, "Jensen Huang" the person.
        queries = dict.fromkeys(
            q
            for q in (subject_query(question), " ".join(question_entities(question)))
            if q
        )
        pages: list[tuple[str, str]] = []
        for query in queries:
            try:
                found = await encyclopedia.search(
                    query[:200], edition, REFERENCE_SECONDS
                )
            except reference_fetch.SourceError:
                continue
            pages += [
                (edition, str(page["title"]))
                for page in found
                if "disambiguation" not in (page.get("pageprops") or {})
            ][:1]
        return list(dict.fromkeys(pages))

    async def gather() -> list[tuple[str, str]]:
        pages = _wiki_pages(urls, passages, edition)[:2]
        pages += [page for page in await searched() if page not in pages]
        found = [
            article
            for article in await asyncio.gather(
                *(one(*page) for page in pages[:REFERENCE_ARTICLES])
            )
            if article is not None
        ]
        others = {article.other for article in found[:1] if article.other}
        others -= set(pages)
        found += [
            article
            for article in await asyncio.gather(*(one(*page) for page in others))
            if article is not None
        ]
        unique = {article.url: article.text for article in found}
        return list(unique.items())[:REFERENCE_ARTICLES]

    try:
        return await asyncio.wait_for(gather(), REFERENCE_SECONDS)
    except TimeoutError:
        LOGGER.info("unsourced_details_reference %s", json.dumps({"error": "late"}))
    except privacy.PrivacyRefusal:
        pass
    return []


def with_sources(answer: str, urls: list[str]) -> str:
    """The articles read for the check join the list of consulted sources."""
    closing = answer.rfind("\n</details>")
    if closing < 0 or "<details>" not in answer:
        return answer
    listed = "".join(f"\n- <{url}>" for url in urls if f"<{url}>" not in answer)
    return answer[:closing] + listed + answer[closing:]


async def review(
    answer: str,
    passages: list[str],
    own_texts: list[str],
    *,
    profile: str,
    language: str,
    question: str = "",
    urls: list[str] | None = None,
    setting: str | None = None,
    reference: bool = True,
) -> tuple[str, Report]:
    """Apply the configured treatment to an answer's unsupported precise details."""
    started = time.monotonic()
    report = Report(mode=setting or mode())
    if report.mode == "off" or not answer.strip():
        return answer, report
    # Today's date is known to the answer (a "consulted on" date is no invention).
    own = Evidence.of([*own_texts, time.strftime("%Y-%m-%d")])
    evidence = Evidence.of(passages)
    report.details = len(extract(answer))
    found = unsupported(answer, evidence, own) if passages else extract(answer)
    if found and reference:
        articles = await references(question, language, urls or [], passages)
        if articles:
            passages = [*passages, *(text for _, text in articles)]
            evidence = Evidence.of(passages)
            checked = unsupported(answer, evidence, own)
            report.referenced = len(found) - len(checked)
            if report.referenced:
                report.reference_urls = [url for url, _ in articles]
                report.reference_texts = [text for _, text in articles]
            found = checked
    if not passages:
        report.revised = "skipped:no_source"
        report.seconds = time.monotonic() - started
        return answer, report
    report.unsupported = report.unsupported_after = len(found)
    if len(found) > max(3, UNCOVERED_SHARE * report.details):
        report.revised = "skipped:uncovered"
        report.seconds = time.monotonic() - started
        LOGGER.info("unsourced_details %s", json.dumps(report.metadata()))
        return with_sources(answer, report.reference_urls), report
    final = answer
    remaining = found
    if found and report.mode in {"revise", "revise_mark"}:
        body, separator, footer = answer.partition("<details>")
        revised = await _revise(
            body.rstrip(), found, passages, profile, language, report
        )
        if revised is not None:
            after = unsupported(revised, evidence, own)
            reason = acceptable(body, revised, found, after)
            if reason:
                report.revised = "rejected:" + reason
            else:
                report.revised = "kept"
                kept = {detail.key for detail in after}
                report.removed = [d.text for d in found if d.key not in kept]
                final = revised + ("\n\n" + separator + footer if separator else "")
                remaining = after
                report.unsupported_after = len(after)
    elif not found:
        report.revised = "skipped:none"
    if remaining and report.mode in {"mark", "revise_mark"}:
        final, report.marked = mark(final, remaining, language)
    final = with_sources(final, report.reference_urls)
    report.seconds = time.monotonic() - started
    LOGGER.info("unsourced_details %s", json.dumps(report.metadata()))
    return final, report
