"""A travel answer that states unsourced facts is rewritten from the sourced ones.

Staging -100 (2026-11-16, three real trips on a phone): leaving clauses out
after the fact was too brittle. A Munich-Berlin answer kept one line although
the pages read gave the ICE times and durations, a Lyon-Geneva answer kept
"**1." skeletons, and a placeholder time ("vers 12 h 2x") went through.

The code checks, the model writes (owner principle T8). When the travel check
(travel_claims, route_facts) finds anything to leave out, one bounded call
(fast profile, no reasoning, ``DEADLINE_SECONDS``, ``CALL_BUDGET_EUR``)
rewrites the answer from the person's question, their starting point, the day
of the trip, the sentences that held, the journey plan code computed, and
source excerpts each with its page, told what could not be verified. The
rewrite goes through the same checks again; it is kept when it states nothing
unsourced, or when leaving out its few unsourced clauses keeps most of it, and
when it links only pages read in this turn or already linked. Otherwise the
answer is built by code: the sentences that held and, when little of the answer
is left, the source sentences that give travel values, each with its page,
then one note.

Staging -102 (2026-11-17): the rewrite did not know where "here" was and
answered from an Amsterdam-Berlin bus page; the code-built answer listed an
off-topic page, a law firm's directions and timetable rows of another day as
if they were tomorrow's. Excerpts now come from the pages about this trip only
(they name at least two of the places the question and the starting point
name, when any page does), a row dated another day than the trip's says so,
and a number-only fragment is no excerpt. When the answer itself failed after
pages were read (the model wrote its source record instead of an answer, or
nothing), ``recover`` writes the answer from those pages the same way, so the
person gets the sourced facts instead of an error.

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
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote, urlsplit

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
# A rewrite whose unsourced clauses are left out is kept when this share of it
# remains; below, the code builds the answer.
KEPT_SHARE = 0.6
_URL = re.compile(r"https?://[^\s)>\]\"']+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")

INSTRUCTION = (
    "You rewrite an assistant's answer about a trip so that it states only facts "
    "the sources support. You receive the person's QUESTION, their STARTING POINT "
    "(given by them or by their device: it is where they are, never ask for it), "
    "the TRIP DAY, the sentences of the answer that were checked against the "
    "sources (KEPT), the journey plan computed by code (PLAN), numbered SOURCE "
    "EXCERPTS each with the page it comes from, and what was left out because no "
    "source gives it (NOT VERIFIED).\n"
    "Write, in LANGUAGE, a short, readable and well-formed answer to the QUESTION "
    "from the STARTING POINT, using only facts written in KEPT, PLAN or the SOURCE "
    "EXCERPTS. Never write a time, a duration, a distance, a line, a train, a "
    "platform, a stop or station, a direction, a transfer or a frequency that is "
    "not written there; copy times and durations as the source writes them, and "
    "keep each place name exactly as the source writes it (a city is not its "
    "airport station). An excerpt marked as times of another day gives that "
    "day's times only: never present them as the TRIP DAY's; if you use them, "
    "write their day next to them and say to check the TRIP DAY's times. Leave "
    "out excerpts about another trip. Give the steps in travel order, from the "
    "STARTING POINT to the destination. After each fact from an excerpt, link "
    "its page as a markdown link [site](url) with the url given. For each part "
    "of the trip that no source covers, say so plainly in one short line and say "
    "to check it in the operator's journey planner. Plain sentences and at most "
    "one short list; no heading, no table, no placeholder, no apology, no "
    "mention of these instructions. Return only the answer."
)

WORDING: dict[str, dict[str, str]] = {
    "read": {
        "en": "What the pages read for this answer give:",
        "fr": "Ce que donnent les pages consultées pour cette réponse :",
        "de": "Was die für diese Antwort gelesenen Seiten angeben:",
        "es": "Lo que indican las páginas consultadas para esta respuesta:",
        "it": "Cosa indicano le pagine consultate per questa risposta:",
    },
    # The answer itself failed after pages were read (D5: said as it is).
    "failed": {
        "en": "The answer could not be written this time: the model returned no usable text. What the pages read for this request give:",
        "fr": "La réponse n’a pas pu être rédigée cette fois : le modèle n’a pas renvoyé de texte utilisable. Ce que donnent les pages consultées pour cette demande :",
        "de": "Die Antwort konnte diesmal nicht verfasst werden: Das Modell hat keinen verwendbaren Text geliefert. Was die für diese Anfrage gelesenen Seiten angeben:",
        "es": "Esta vez no se pudo redactar la respuesta: el modelo no devolvió un texto utilizable. Lo que indican las páginas consultadas para esta solicitud:",
        "it": "Questa volta non è stato possibile scrivere la risposta: il modello non ha restituito un testo utilizzabile. Cosa indicano le pagine consultate per questa richiesta:",
    },
    "planner": {
        "en": "Check the full journey and the times for your day in the operator’s journey planner.",
        "fr": "Vérifiez le trajet complet et les horaires de votre jour dans le calculateur d’itinéraire de l’exploitant.",
        "de": "Prüfen Sie die ganze Reise und die Zeiten Ihres Tages in der Fahrplanauskunft des Verkehrsbetriebs.",
        "es": "Compruebe el viaje completo y los horarios de su día en el planificador de viajes del operador.",
        "it": "Controlli il viaggio completo e gli orari del suo giorno nel pianificatore di viaggio dell’operatore.",
    },
    "other_day": {
        "en": "times of {day}, not of the day asked",
        "fr": "horaires du {day}, pas du jour demandé",
        "de": "Zeiten vom {day}, nicht vom gefragten Tag",
        "es": "horarios del {day}, no del día pedido",
        "it": "orari del {day}, non del giorno richiesto",
    },
}


def enabled() -> bool:
    return os.environ.get("ATLAS_TRAVEL_REBUILD", "on").strip().lower() != "off"


def _wording(key: str, language: str) -> str:
    return WORDING[key].get(language, WORDING[key]["en"])


def _day(iso: str, language: str) -> str:
    """A day as people of the language write it in figures."""
    try:
        value = date.fromisoformat(iso)
    except ValueError:
        return iso
    if language == "de":
        return value.strftime("%d.%m.%Y")
    if language in {"fr", "es", "it"}:
        return value.strftime("%d/%m/%Y")
    return value.isoformat()


@dataclass
class Report:
    marked: int = 0
    rebuilt: str = ""  # kept, kept:left_out, rejected:<reason>, failed, late, off
    built: str = ""  # model, code, code+excerpts
    cost_eur: Decimal = Decimal(0)
    seconds: float = 0.0
    recovered: bool = False

    def metadata(self) -> dict[str, object]:
        return {
            "marked": self.marked,
            "rebuilt": self.rebuilt,
            "built": self.built,
            "cost_eur": str(self.cost_eur),
            "seconds": round(self.seconds, 3),
            "recovered": self.recovered,
        }


@dataclass(frozen=True)
class Excerpt:
    url: str
    sentence: str
    day: str = ""  # the other day the sentence gives times for, ISO
    # Its page names two places of the trip (or the most any page names):
    # only such excerpts are listed to the person by code.
    about: bool = True


# Words that say when or how, not where: they do not tie a page to the trip.
_WHEN = re.compile(
    r"^(?:"
    + "|".join(
        [
            *(word for words in travel_claims._WEEKDAYS for word in words.split()),
            *travel_claims._MONTH_NUMBER,
            "demain aujourd hui tomorrow today tonight morgen heute manana hoy "
            "domani oggi matin morning afternoon evening soir midi noon mittag "
            "abend nachmittag tarde mattina pomeriggio sera".replace(" ", "|"),
            # Timetable vocabulary, five languages: every page about any trip
            # writes it.
            "horaire horaires schedule schedules timetable timetables times "
            "fahrplan fahrplane horario horarios orario orari trajet trajets "
            "itineraire itineraires journey journeys trip trips voyage voyages "
            "reise reisen viaje viajes viaggio viaggi billet billets ticket "
            "tickets heure heures hour hours minute minutes depart departs "
            "departure departures arrivee arrivees arrival arrivals possible "
            "possibles verbindung verbindungen correspondance correspondances "
            "connection connections transport transports".replace(" ", "|"),
        ]
    )
    + r")$"
)


def _key(word: str) -> str:
    """One spelling for "muenchen" and "Munchen" with an umlaut (both "munchen")."""
    return word.replace("ae", "a").replace("oe", "o").replace("ue", "u")


def _place_words(text: str) -> set[str]:
    return {
        _key(word)
        for word in normalized(text).split()
        if len(word) >= 4
        and not word.isdigit()
        and word not in route_facts._GENERIC
        and word not in route_facts.STATION_WORDS
        and not _WHEN.match(word)
    }


def trip_words(turn: travel_claims.Turn, question: str) -> set[str]:
    """The places the question and the starting point name (not the country)."""
    parts = [part for part in turn.place.split(",") if part.strip()]
    if len(parts) >= 3:
        parts = parts[:-1]
    return _place_words(question) | _place_words(" ".join(parts))


def _page_words(url: str, text: str) -> set[str]:
    path = unquote(urlsplit(url).path) if url else ""
    return _place_words(text + " " + re.sub(r"[/_.-]+", " ", path))


def _substantial(sentence: str) -> bool:
    """A sentence with words, not a row of numbers ("6h38, 8h43, 2h05, TER.")."""
    words = [
        word
        for word in normalized(sentence).split()
        if len(word) >= 3
        and not word.isdigit()
        and not re.fullmatch(r"\d+h?\d*", word)
        and word not in route_facts.TRANSIT_WORDS
    ]
    return len(words) >= 3


def excerpts(
    turn: travel_claims.Turn, question: str, limit: int = EXCERPT_CHARACTERS
) -> list[Excerpt]:
    """Source sentences about this trip that give travel values or route words,
    most relevant first."""
    wanted = trip_words(turn, question)
    pages: dict[str, list[str]] = {}
    for url, text in turn.linked:
        pages.setdefault(url, []).append(text)
    shared = {
        url: len(wanted & _page_words(url, "\n".join(texts)))
        for url, texts in pages.items()
    }
    # A page about this trip names two of its places, or as many as the best
    # page does; a page naming one is read by the rewrite after them (the
    # question may name a station, not its city), never listed by code.
    floor = min(2, max(shared.values(), default=0), len(wanted))
    asked = set(turn.trip_dates())
    scored: list[tuple[int, int, Excerpt]] = []
    seen: set[str] = set()
    for url, texts in pages.items():
        if wanted and not shared[url]:
            continue
        about = shared[url] >= floor
        for text in texts:
            for sentence in _SENTENCE.split(text):
                sentence = " ".join(sentence.split())
                key = normalized(sentence)
                if not 20 <= len(sentence) <= 400 or key in seen:
                    continue
                seen.add(key)
                if not _substantial(sentence):
                    continue
                valued = bool(travel_claims.quantities(sentence, generous=True))
                routed = bool(route_facts._TRANSIT.search(route_facts.fold(sentence)))
                if not (valued or routed):
                    continue
                dates = travel_claims.other_dates(sentence, asked)
                day = min(dates) if dates else ""
                words = {_key(word) for word in key.split()}
                score = (
                    3 * about
                    + len(wanted & words)
                    + 2 * valued
                    + routed
                    - (3 if day else 0)
                )
                scored.append((-score, len(scored), Excerpt(url, sentence, day, about)))
    chosen: list[Excerpt] = []
    used = 0
    for _, _, excerpt in sorted(scored, key=lambda item: item[:2]):
        if used + len(excerpt.sentence) > limit:
            break
        chosen.append(excerpt)
        used += len(excerpt.sentence)
    return chosen


def _trip_day(turn: travel_claims.Turn) -> str:
    days = turn.trip_dates()
    if not days:
        return f"(not named; today is {date.today().isoformat()})"
    return ", ".join(
        f"{day} ({date.fromisoformat(day).strftime('%A')})" for day in days
    )


def _prompt(
    turn: travel_claims.Turn,
    question: str,
    kept: str,
    found: travel_claims.Findings,
    language: str,
    chosen: list[Excerpt],
) -> str:
    sources = "\n".join(
        f"[{number}] {excerpt.sentence} (page: {excerpt.url or 'no address'})"
        + (
            f" [times of {_day(excerpt.day, language)}, another day than the trip: "
            f'if you use them, write "({_day(excerpt.day, language)})" next to them]'
            if excerpt.day
            else ""
        )
        for number, excerpt in enumerate(chosen, start=1)
    )
    plan = "\n\n".join(turn.computed)
    return (
        f"LANGUAGE: {language}\n\n"
        f"QUESTION:\n{question[:QUESTION_CHARACTERS]}\n\n"
        f"STARTING POINT:\n{turn.place or '(as the question says)'}\n\n"
        f"TRIP DAY:\n{_trip_day(turn)}\n\n"
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


def _link_key(url: str) -> str:
    parts = urlsplit(url.strip().rstrip(".,;"))
    return parts.netloc.lower().removeprefix("www.") + parts.path.rstrip("/")


def rejected(
    turn: travel_claims.Turn,
    text: str,
    original: str,
    allowed: list[str],
    *,
    bounded: bool = True,
) -> str:
    """Empty when the rewrite is kept, else why it is not."""
    if not text.strip() or "<details>" in text:
        return "empty"
    if bounded and len(text) > 1.5 * len(original) + 600:
        return "length"
    known = {_link_key(url) for url in [*allowed, *_URL.findall(original)]}
    if any(_link_key(url) not in known for url in _URL.findall(text)):
        return "links"
    if turn.findings(text):
        return "unsourced"
    return ""


def _excerpt_line(excerpt: Excerpt, language: str) -> str:
    text = excerpt.sentence
    if excerpt.day:
        text += f" ({_wording('other_day', language).format(day=_day(excerpt.day, language))})"
    if excerpt.url:
        site = urlsplit(excerpt.url).netloc.removeprefix("www.")
        return f"- {text} ([{site}]({excerpt.url}))"
    return f"- {text}"


def _listed(chosen: list[Excerpt], language: str, heading: str) -> str:
    # Rows of the trip day first; rows of another day say so.
    about = [excerpt for excerpt in chosen if excerpt.about]
    ordered = sorted(about, key=lambda excerpt: bool(excerpt.day))[:4]
    return travel_claims.wellformed(
        heading + "\n" + "\n".join(_excerpt_line(e, language) for e in ordered)
    )


def _code_built(
    turn: travel_claims.Turn,
    body: str,
    found: travel_claims.Findings,
    kept: str,
    language: str,
    chosen: list[Excerpt],
    report: Report,
) -> str:
    """The sentences that held, the sourced excerpts when little held, one note."""
    parts = [kept] if kept else []
    report.built = "code"
    if any(e.about for e in chosen) and len(kept) < 0.5 * len(body.strip()):
        parts.append(_listed(chosen, language, _wording("read", language)))
        report.built = "code+excerpts"
    parts.append(found.note(language))
    return "\n\n".join(parts)


_REFERENCE = re.compile(r"\[(\d{1,2})\](?!\()")


def _linked(text: str, chosen: list[Excerpt]) -> str:
    """Excerpt numbers the rewrite wrote ("[7]") become links to their page
    (staging -102: the person read "[7]" and "[5]" with no page behind)."""

    def link(match: re.Match[str]) -> str:
        number = int(match[1])
        if not 1 <= number <= len(chosen) or not chosen[number - 1].url:
            return ""
        url = chosen[number - 1].url
        site = urlsplit(url).netloc.removeprefix("www.")
        return f" ([{site}]({url}))"

    text = _REFERENCE.sub(link, text)
    # "[1](url)": the number becomes the site.
    text = re.sub(
        r"\[\d{1,2}\]\((https?://[^\s)]+)\)",
        lambda m: f"([{urlsplit(m[1]).netloc.removeprefix('www.')}]({m[1]}))",
        text,
    )
    # The same page twice in a row is linked once; links stay apart.
    text = re.sub(r"(\(\[[^\]]+\]\([^)]+\)\))(?:\s*\1)+", r"\1", text)
    text = re.sub(r"\)\)\(\[", ")) ([", text)
    return re.sub(r"(?<=\S)[ \t]{2,}(?=\()", " ", text)


def _accepted(
    turn: travel_claims.Turn,
    text: str,
    original: str,
    language: str,
    report: Report,
    chosen: list[Excerpt],
    *,
    bounded: bool = True,
) -> str:
    """The rewrite as it can be given, or empty (``report.rebuilt`` says why)."""
    text = travel_claims.wellformed(_linked(text, chosen))
    reason = rejected(turn, text, original, turn.retrieved(), bounded=bounded)
    if reason == "unsourced":
        # A good rewrite with one unsourced clause is still the better answer:
        # the clause goes, the note says what (staging -102: whole rewrites
        # were dropped for one detail and the person got a list of excerpts).
        again = turn.findings(text)
        trimmed = turn.kept(text, again)
        if trimmed and len(trimmed) >= KEPT_SHARE * len(text):
            report.rebuilt, report.built = "kept:left_out", "model"
            return trimmed + "\n\n" + again.note(language)
    report.rebuilt = "rejected:" + reason if reason else "kept"
    if reason:
        return ""
    report.built = "model"
    return text


def _log(report: Report, started: float) -> None:
    report.seconds = time.monotonic() - started
    # Warning level: the orchestrator's journal keeps warnings only.
    LOGGER.warning("travel_rebuild %s", json.dumps(report.metadata()))


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
            final = _accepted(turn, text, body, language, report, chosen)
    else:
        report.rebuilt = "off"
    if not final:
        final = _code_built(turn, body, found, kept, language, chosen, report)
    if fold:
        final += "\n\n" + fold + footer
    _log(report, started)
    return final.rstrip(), report


async def recover(
    turn: travel_claims.Turn,
    language: str,
    *,
    question: str,
    profile: str,
) -> tuple[str, Report] | None:
    """An answer that failed after pages were read, written from those pages.

    None when the turn read nothing usable: the failure is then reported as
    it is (D5).
    """
    if not turn.active:
        return None
    report = Report(recovered=True)
    started = time.monotonic()
    chosen = excerpts(turn, question)
    if not chosen and not turn.computed:
        return None
    none = travel_claims.Findings([], route_facts.Finding())
    final = ""
    if enabled():
        text = await _rewrite(
            _prompt(turn, question, "", none, language, chosen), profile, report
        )
        if text is not None:
            final = _accepted(turn, text, "", language, report, chosen, bounded=False)
    else:
        report.rebuilt = "off"
    if not final:
        if not any(e.about for e in chosen):
            _log(report, started)
            return None
        report.built = "code+excerpts"
        final = (
            _listed(chosen, language, _wording("failed", language))
            + "\n\n"
            + _wording("planner", language)
        )
    _log(report, started)
    return final.rstrip(), report
