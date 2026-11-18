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
from services.orchestrator import (
    deadline,
    redirects,
    route_facts,
    travel_claims,
    unsourced_details,
)
from services.orchestrator.dictation_cleanup import fast_profile
from services.orchestrator.grounding import normalized

LOGGER = logging.getLogger(__name__)
DEADLINE_SECONDS = 15.0
CALL_BUDGET_EUR = Decimal("0.02")
MAX_TOKENS = 900
EXCERPT_CHARACTERS = 3500
QUESTION_CHARACTERS = 1500
# A rewrite whose unsourced clauses are left out is kept when this share of it
# remains; below, the code builds the answer.
KEPT_SHARE = 0.6
# A trimmed rewrite this long still answers better than code (staging -110).
MIN_TRIMMED = 160
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
    "mention of these instructions, never the name of a section (KEPT, PLAN, "
    "NOT VERIFIED) in place of a link. Return only the answer."
)

WORDING: dict[str, dict[str, str]] = {
    # The answer itself failed after pages were read (D5: said as it is).
    "failed": {
        "en": "The answer could not be written this time: the model returned no usable text.",
        "fr": "La réponse n’a pas pu être rédigée cette fois : le modèle n’a pas renvoyé de texte utilisable.",
        "de": "Die Antwort konnte diesmal nicht verfasst werden: Das Modell hat keinen verwendbaren Text geliefert.",
        "es": "Esta vez no se pudo redactar la respuesta: el modelo no devolvió un texto utilizable.",
        "it": "Questa volta non è stato possibile scrivere la risposta: il modello non ha restituito un testo utilizzabile.",
    },
    # The pages about this trip, as links: never their raw snippets (staging
    # -110: a list of snippets, some in another language than the answer).
    "pages": {
        "en": "The pages read about this trip give its timetable: {links}. Check the times for your day there.",
        "fr": "Les pages consultées sur ce trajet en donnent les horaires : {links}. Vérifiez-y les horaires de votre jour.",
        "de": "Die gelesenen Seiten zu dieser Reise geben ihren Fahrplan an: {links}. Prüfen Sie dort die Zeiten Ihres Tages.",
        "es": "Las páginas consultadas sobre este viaje dan sus horarios: {links}. Compruebe allí los horarios de su día.",
        "it": "Le pagine consultate su questo viaggio ne danno gli orari: {links}. Controlli lì gli orari del suo giorno.",
    },
    "planner": {
        "en": "Check the full journey and the times for your day in the operator’s journey planner.",
        "fr": "Vérifiez le trajet complet et les horaires de votre jour dans le calculateur d’itinéraire de l’exploitant.",
        "de": "Prüfen Sie die ganze Reise und die Zeiten Ihres Tages in der Fahrplanauskunft des Verkehrsbetriebs.",
        "es": "Compruebe el viaje completo y los horarios de su día en el planificador de viajes del operador.",
        "it": "Controlli il viaggio completo e gli orari del suo giorno nel pianificatore di viaggio dell’operatore.",
    },
    # A connection the pages give for the times the answer uses.
    "connection": {
        "en": "According to the pages read, this trip includes a connection{at}: allow for it and check it in the operator’s journey planner.",
        "fr": "D’après les pages consultées, ce trajet comprend une correspondance{at} : prévoyez-la et vérifiez-la dans le calculateur d’itinéraire de l’exploitant.",
        "de": "Laut den gelesenen Seiten enthält diese Reise einen Umstieg{at}: Planen Sie ihn ein und prüfen Sie ihn in der Fahrplanauskunft des Verkehrsbetriebs.",
        "es": "Según las páginas consultadas, este viaje incluye un transbordo{at}: téngalo en cuenta y compruébelo en el planificador de viajes del operador.",
        "it": "Secondo le pagine consultate, questo viaggio comprende un cambio{at}: ne tenga conto e lo controlli nel pianificatore di viaggio dell’operatore.",
    },
    "connection_at": {
        "en": " at {place}",
        "fr": " à {place}",
        "de": " in {place}",
        "es": " en {place}",
        "it": " a {place}",
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


# Street types, five languages: a starting point's street type names no place.
_STREETS = frozenset(
    (
        "street road avenue lane drive boulevard place square terrace court "
        "highway parkway rue avenue chemin allee impasse quai cours route "
        "strasse weg gasse platz allee ring damm ufer calle avenida paseo plaza "
        "camino carrer ronda carretera via viale piazza corso vicolo largo"
    ).split()
)


def _capitalized(texts: list[str]) -> set[str]:
    """Names: words written with a capital after a lower-case word more often
    than in lower case ("Sydney", never "flight")."""
    upper: dict[str, int] = {}
    lower: dict[str, int] = {}
    for text in texts:
        previous = ""
        for match in re.finditer(r"[^\W\d_]++", text):
            word = match[0]
            if word[:1].islower():
                for key in _place_words(word):
                    lower[key] = lower.get(key, 0) + 1
            elif previous[:1].islower():
                for key in _place_words(word):
                    upper[key] = upper.get(key, 0) + 1
            previous = word
    return {key for key, count in upper.items() if count > lower.get(key, 0)}


def _trip_names(
    turn: travel_claims.Turn, question: str, answer: str
) -> tuple[set[str], list[set[str]]]:
    """The places of this trip: the starting point's words (its street type
    and country left out), the question's words the pages or the person write
    as names, and the answer's place names of two words or more."""
    parts = [part for part in turn.place.split(",") if part.strip()]
    if len(parts) >= 3:
        parts = parts[:-1]
    names = _place_words(" ".join(parts))
    names |= _place_words(question) & _capitalized(
        [question, *(text for _, text in turn.linked)]
    )
    names = {w for w in names if w not in route_facts.PLACE_WORDS | _STREETS}
    phrases = [
        words
        for mention in route_facts.mentions(answer, answer)
        if len(words := _place_words(mention) - route_facts.PLACE_WORDS - _STREETS) >= 2
    ]
    return names, phrases


def elsewhere(turn: travel_claims.Turn, question: str, answer: str) -> set[str]:
    """The pages read that are about another place (page keys): they name no
    place of this trip while another page does.

    Staging -118: a Sydney airport answer cited Brisbane's airport train for
    "the station access fee is included in the fare", and the sources listed
    Perth's and Bangkok's airport trains. Such a page supports no fact of the
    trip's answer, is no excerpt for its rewrite and is not listed.
    """
    names, phrases = _trip_names(turn, question, answer)
    if not names and not phrases:
        return set()
    texts: dict[str, list[str]] = {}
    for url, text in turn.linked:
        if url:
            texts.setdefault(url, []).append(text)

    def about(url: str, words: set[str]) -> bool:
        host = urlsplit(url).netloc.lower()
        return (
            bool(words & names)
            or any(name in host for name in names)
            or any(len(phrase & words) >= 2 for phrase in phrases)
        )

    named = {
        url
        for url, found in texts.items()
        if about(url, _page_words(url, "\n".join(found)))
    }
    if not named:
        return set()
    return {travel_claims.page_key(url) for url in set(texts) - named}


def _pages(
    turn: travel_claims.Turn, question: str, answer: str
) -> tuple[set[str], dict[str, list[str]], dict[str, int], int]:
    """The trip's place words, the texts read by page (none about another
    trip), how many of those words each page names, and how many a page about
    this trip names."""
    wanted = trip_words(turn, question)
    elsewhere = off_route(turn, question, answer)
    pages: dict[str, list[str]] = {}
    for url, text in turn.pages():
        if url not in elsewhere:
            pages.setdefault(url, []).append(text)
    shared = {
        url: len(wanted & _page_words(url, "\n".join(texts)))
        for url, texts in pages.items()
    }
    # A page about this trip names two of its places, or as many as the best
    # page does; a page naming one is read by the rewrite after them (the
    # question may name a station, not its city), never listed by code.
    floor = min(2, max(shared.values(), default=0), len(wanted))
    return wanted, pages, shared, floor


def unrelated(turn: travel_claims.Turn, question: str, answer: str) -> set[str]:
    """The pages read that are not about this trip: about another trip, or
    naming fewer of its places than a page about it does (the rule by which
    code lists pages, ``excerpts``).

    Staging -116: the consulted-sources list under a Sydney airport trip held
    a Gatwick shuttle, a Vienna airport train, a New Delhi station and flight
    listings, every search hit of the turn. A trip answer's list leaves such
    pages out unless the answer cites them; they stay in the turn's record.
    """
    if not turn.active:
        return set()
    wanted, pages, shared, floor = _pages(turn, question, answer)
    if not wanted:
        return set()
    urls = {url for url, _ in turn.linked if url}
    return (urls - set(pages)) | {url for url, count in shared.items() if count < floor}


def excerpts(
    turn: travel_claims.Turn,
    question: str,
    limit: int = EXCERPT_CHARACTERS,
    answer: str = "",
) -> list[Excerpt]:
    """Source sentences about this trip that give travel values or route words,
    most relevant first; never from a page about another trip."""
    wanted, pages, shared, floor = _pages(turn, question, answer)
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
    # Never past the turn's own deadline: a rewrite cut by it would take the
    # answer it improves with it.
    left = deadline.remaining()
    seconds = DEADLINE_SECONDS if left is None else min(DEADLINE_SECONDS, left - 1)
    if seconds <= 1:
        report.rebuilt = "late"
        return None
    payload["timeout"] = seconds
    try:
        result = await asyncio.wait_for(unsourced_details._complete(payload), seconds)
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
    return unlabelled(str(result["text"]))


# The rewrite's input sections: a reference to one is no source the person can
# open ("(KEPT)", "(PLAN)"). An excerpt number ("[3]") becomes a link later.
_NAMED = r"(?:KEPT|PLAN|NOT[ \t]VERIFIED|SOURCE[ \t]EXCERPTS?)"
_LABEL = re.compile(
    rf"[ \t]*+(?:\({_NAMED}(?:[ \t]*+[,;&][ \t]*+{_NAMED})*+\)"
    rf"|\[{_NAMED}(?:[ \t]*+[,;&][ \t]*+{_NAMED})*+\](?!\())"
)
_SOURCE_NUMBER = re.compile(
    r"[\[(]SOURCE(?:[ \t]EXCERPTS?)?[ \t]*+(\d{1,2}+)[\])](?!\()"
)


def unlabelled(text: str) -> str:
    """The rewrite without references to its input sections; "(SOURCE 3)" is
    the excerpt number "[3]".

    Staging -120: a Munich - Berlin answer cited "(KEPT)" three times, the
    name of the prompt section its facts came from, in place of a page.
    """
    return _LABEL.sub("", _SOURCE_NUMBER.sub(r"[\1]", text)).strip()


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


def _pointer(chosen: list[Excerpt], language: str) -> str:
    """One sentence linking the pages about this trip, at most three."""
    urls = list(dict.fromkeys(e.url for e in chosen if e.about and e.url))[:3]
    if not urls:
        return ""
    links = ", ".join(
        f"[{urlsplit(url).netloc.removeprefix('www.')}]({url})" for url in urls
    )
    return _wording("pages", language).format(links=links)


def _code_built(
    turn: travel_claims.Turn,
    body: str,
    found: travel_claims.Findings,
    kept: str,
    language: str,
    chosen: list[Excerpt],
    report: Report,
) -> str:
    """The sentences that held, the pages about the trip when little held, one
    note."""
    parts = [kept] if kept else []
    report.built = "code"
    pointer = _pointer(chosen, language)
    if pointer and len(kept) < 0.5 * len(body.strip()):
        parts.append(pointer)
        report.built = "code+pages"
    parts.append(found.note(language))
    return "\n\n".join(part for part in parts if part)


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
    text = re.sub(r"(?<=\S)[ \t]{2,}(?=\()", " ", text)
    return unwrapped(text)


_CITED = r"\(\[[^\]\n]++\]\([^)\s]++\)\)"
# One link, or a group of links joined by commas, in parentheses again.
_WRAPPED = re.compile(rf"\([ \t]*+({_CITED}(?:[ \t]*+[,;][ \t]*+{_CITED})*+)[ \t]*+\)")


def unwrapped(text: str) -> str:
    """A link the rewrite had already put in parentheses is not wrapped twice
    (staging -104: "(([content.amtrak.com](...)))" after every fact), nor a
    group of links (staging -109: "(([renfe.com](...)), ([ouigo.com](...)))")."""
    previous = None
    while previous != text:
        previous, text = text, _WRAPPED.sub(r"\1", text)
    return text


_ONE = r"\(\[([^\]\n]++)\]\([^)\s]++\)\)"
# The same site cited again right after itself, with or without a separator.
_REPEATED = re.compile(rf"({_ONE})(?:[ \t]*+[,;]?[ \t]*+\(\[\2\]\([^)\s]++\)\))++")
# Separators left doubled or dangling when a link between them was removed.
_DOUBLED = re.compile(r"(?<=\S)[ \t]*+[,;][ \t]*+(?:[,;][ \t]*+)++")
_DANGLING = re.compile(r"[ \t]*+[,;][ \t]*+(?=[.!?)](?:\s|$))")
_OPENING = re.compile(r"\([ \t]*+[,;][ \t]*+")


# A page written as "[source: url]" in any language ("[fuente: ...]",
# "(Quelle: ...)"), or as a bare url in brackets.
_WORDED = re.compile(
    r"(?<!\])[\[(][ \t]*+(?:[^\W\d_]{2,12}+[ \t]*+:[ \t]*+)?(https?://[^\s\])]++)[ \t]*+[\])]"
)
# Links side by side inside one pair of parentheses.
_GROUP = re.compile(
    r"\((\[[^\]\n]++\]\([^)\s]++\)(?:[ \t]*+[,;][ \t]*+\[[^\]\n]++\]\([^)\s]++\))++)\)"
)
_IN_GROUP = re.compile(r"\[([^\]\n]++)\]\([^)\s]++\)")


def _site(url: str) -> str:
    return f"([{urlsplit(url).netloc.removeprefix('www.')}]({url}))"


def _one_per_site(match: re.Match[str]) -> str:
    """One link per site in a group: "(amtrakguide.com, amtrakguide.com)" on
    staging -120 was two pages of one site."""
    seen: set[str] = set()
    links: list[str] = []
    for link in _IN_GROUP.finditer(match[1]):
        if link[1].lower() not in seen:
            seen.add(link[1].lower())
            links.append(link[0])
    return "(" + ", ".join(links) + ")"


def citations(text: str) -> str:
    """Citations joined once (staging -112: "(bahn.de),, (sncf-connect.com)"
    after a link between them went, "(amtrak.com) (amtrak.com)"): no doubled
    or dangling separator, one citation of a site where it follows itself."""
    lines: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and not line.lstrip().startswith("|"):
            # Staging -120 (Madrid): "[fuente: https://...]" after each fact.
            line = _WORDED.sub(lambda m: _site(m[1]), line)
            line = _GROUP.sub(_one_per_site, line)
            line = _REPEATED.sub(r"\1", line)
            line = _DOUBLED.sub(", ", line)
            line = _DANGLING.sub("", line)
            line = _OPENING.sub("(", line)
        lines.append(line)
    return "\n".join(lines)


_ANY_LINK = re.compile(r"\[([^\]\n]++)\]\((https?://[^)\s]++)\)")


def _read_links_only(text: str, allowed: list[str], original: str) -> str:
    """A link to a page not read in this turn keeps its words, not its address
    (staging -110: whole rewrites were dropped for one operator link)."""
    known = {_link_key(url) for url in [*allowed, *_URL.findall(original)]}

    def keep(match: re.Match[str]) -> str:
        return match[0] if _link_key(match[2]) in known else match[1]

    text = _ANY_LINK.sub(keep, text)
    bare = [url for url in _URL.findall(text) if _link_key(url) not in known]
    for url in bare:
        text = text.replace(url, "")
    return unwrapped(re.sub(r"\(\s*\)", "", text))


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
    text = travel_claims.wellformed(
        _read_links_only(_linked(text, chosen), turn.retrieved(), original)
    )
    reason = rejected(turn, text, original, turn.retrieved(), bounded=bounded)
    if reason == "unsourced":
        # A good rewrite with one unsourced clause is still the better answer:
        # the clause goes, the note says what (staging -102: whole rewrites
        # were dropped for one detail and the person got a list of excerpts).
        again = turn.findings(text)
        trimmed = turn.kept(text, again)
        if trimmed and (
            len(trimmed) >= KEPT_SHARE * len(text) or len(trimmed) >= MIN_TRIMMED
        ):
            report.rebuilt, report.built = "kept:left_out", "model"
            return "\n\n".join(
                p
                for p in (
                    trimmed,
                    _uncited(trimmed, chosen, language),
                    again.note(language),
                )
                if p
            )
    report.rebuilt = "rejected:" + reason if reason else "kept"
    if reason:
        return ""
    report.built = "model"
    return "\n\n".join(p for p in (text, _uncited(text, chosen, language)) if p)


def _uncited(text: str, chosen: list[Excerpt], language: str) -> str:
    """The pages about the trip, when the answer links none of the pages read.

    Staging -118: a Villeurbanne - Geneva airport rewrite kept only "the rest
    of the trip is not covered by the sources" although the operators' pages
    about the trip were read; the person is given those pages at least.
    """
    if re.search(r"\]\(https?://", text):
        return ""
    return _pointer(chosen, language)


def _log(report: Report, started: float) -> None:
    report.seconds = time.monotonic() - started
    # Warning level: the orchestrator's journal keeps warnings only.
    LOGGER.warning("travel_rebuild %s", json.dumps(report.metadata()))


# Staging -104: Caen-Paris trains reach Paris Saint-Lazare; the answer gave
# their times as arrivals "at Gare de Lyon" without the change of station the
# page gave. A connection a source states next to the times the answer uses is
# said, with its station when the source names it. Transit vocabulary of the
# five interface languages, never places.
_TRANSFER = re.compile(
    r"(?<![\w-])(?:correspondances?|changements?|connections?|connecting|"
    r"transfers?|change (?:at|in)|umstieg\w*+|umsteig\w*+|transbordos?|"
    r"coincidencias?|cambio (?:a|in)|cambi di treno)(?![\w-])"
)
_NO_TRANSFER = re.compile(
    r"(?:sans|without|no|ohne|sin|senza|zero|aucune?|kein\w*+|0)[ \t]++(?:\w++[ \t]++)?$"
)
_TRANSFER_PLACE = re.compile(
    r"^[ \t]*+(?:[a-z]++[ \t]++)?(?:a|à|at|in|en|via|au)[ \t]++"
    r"((?:[A-ZÀ-Ý][\w'’-]*+)(?:[ \t]++(?:[A-ZÀ-Ý][\w'’-]*+|de|du|la|le|del))*+)"
)
CONNECTION_WINDOW = 300


def connection_note(turn: travel_claims.Turn, answer: str, language: str) -> str:
    """One line when the pages give a connection the answer does not state."""
    if not turn.active or not answer.strip():
        return ""
    body = answer.partition("<details>")[0]
    used = [
        q.value
        for q in travel_claims.quantities(body)
        if q.kind == "clock" and q.value == q.value
    ]
    if not used:
        return ""
    stated = bool(_TRANSFER.search(route_facts.fold(body)))
    folded_body = normalized(body)
    for _, text in turn.linked:
        folded = route_facts.fold(text)
        for q in travel_claims.quantities(text, generous=True):
            if q.kind != "clock" or not any(abs(q.value - v) < 0.5 for v in used):
                continue
            start = max(0, q.start - CONNECTION_WINDOW)
            for match in _TRANSFER.finditer(folded, start, q.end + CONNECTION_WINDOW):
                if _NO_TRANSFER.search(
                    folded[max(0, match.start() - 20) : match.start()]
                ):
                    continue
                place = _TRANSFER_PLACE.match(text[match.end() : match.end() + 80])
                name = place[1].strip() if place else ""
                if name and normalized(name) in folded_body:
                    continue
                if not name and stated:
                    continue
                at = (
                    _wording("connection_at", language).format(place=name)
                    if name
                    else ""
                )
                return _wording("connection", language).format(at=at)
    return ""


def _with_connection(turn: travel_claims.Turn, answer: str, language: str) -> str:
    note = connection_note(turn, answer, language)
    if not note:
        return answer
    body, fold, footer = answer.partition("<details>")
    return body.rstrip() + "\n\n" + note + ("\n\n" + fold + footer if fold else "")


# Staging -106: a Madrid-Barcelona answer linked a Madrid-Sevilla timetable as
# its source. A page whose address names a trip between two places (a
# hyphenated pair, "madrid-sevilla", or its last two path segments,
# "muenchen/berlin") is about another trip when one of the two is a place of
# the question or the starting point and the other appears neither there nor
# in the answer; when another page read names this trip's pair, such a page is
# no source for this answer.
_LINK = re.compile(
    r"[ \t]*+\(\[[^\]\n]++\]\((https?://[^)\s]++)\)\)|\[([^\]\n]++)\]\((https?://[^)\s]++)\)"
)


def _url_pairs(url: str) -> list[tuple[str, str]]:
    path = unquote(urlsplit(url).path).lower().strip("/")
    segments = [re.sub(r"\.[a-z0-9]{2,5}$", "", s) for s in path.split("/") if s]
    pairs: list[tuple[str, str]] = []
    for segment in segments:
        words = [w for w in segment.split("-") if _place_words(w)]
        keys = [_key(normalized(w)) for w in words]
        pairs += list(zip(keys, keys[1:], strict=False))
    tail = segments[-2:]
    if len(tail) == 2 and all(re.fullmatch(r"[a-z]{4,}", s) for s in tail):
        if all(_place_words(s) for s in tail):
            pairs.append((_key(tail[0]), _key(tail[1])))
    return pairs


def off_route(turn: travel_claims.Turn, question: str, answer: str) -> set[str]:
    """The pages read that are about another trip than this one."""
    wanted = trip_words(turn, question)
    known = wanted | _place_words(answer)
    urls = {url for url, _ in turn.linked if url}
    pairs = {url: _url_pairs(url) for url in urls}
    if not any(a in known and b in known for found in pairs.values() for a, b in found):
        return set()
    return {
        url
        for url, found in pairs.items()
        if any(
            (a in wanted) != (b in wanted) and not (a in known and b in known)
            for a, b in found
        )
        and not any(a in known and b in known for a, b in found)
    }


def _without(answer: str, urls: set[str]) -> str:
    """The answer without its links to the pages in ``urls``."""
    if not urls:
        return answer
    gone = {_link_key(url) for url in urls}

    def keep(match: re.Match[str]) -> str:
        url = match[1] or match[3]
        if _link_key(url) not in gone:
            return match[0]
        return "" if match[1] else match[2]

    body, fold, footer = answer.partition("<details>")
    return _LINK.sub(keep, body) + fold + footer


async def review(
    turn: travel_claims.Turn,
    answer: str,
    language: str,
    *,
    question: str,
    profile: str,
) -> tuple[str, Report]:
    """The travel answer with nothing unsourced (rewritten, or built by code),
    no link to a page about another trip, and the connection the pages give
    when it does not say it."""
    if turn.active:
        turn.excluded = elsewhere(turn, question, answer.partition("<details>")[0])
    final, report = await _review(
        turn, answer, language, question=question, profile=profile
    )
    final = _without(final, off_route(turn, question, final.partition("<details>")[0]))
    body, fold, footer = final.partition("<details>")
    body = redirects.claims_without(
        body, lambda url: travel_claims.page_key(url) in turn.excluded
    )
    body = travel_claims.outliers_left_out(redirects.forum_claims(unwrapped(body)))
    final = citations(redirects.cited(body)) + fold + footer
    return _with_connection(turn, final, language), report


async def _review(
    turn: travel_claims.Turn,
    answer: str,
    language: str,
    *,
    question: str,
    profile: str,
) -> tuple[str, Report]:
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
    chosen = excerpts(turn, question, answer=body)
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
    turn.excluded = elsewhere(turn, question, "")
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
        report.built = "code+pages"
        final = "\n\n".join(
            part
            for part in (
                _wording("failed", language),
                _pointer(chosen, language),
                _wording("planner", language),
            )
            if part
        )
    _log(report, started)
    final = citations(redirects.cited(redirects.forum_claims(final.rstrip())))
    return _with_connection(turn, final, language), report
