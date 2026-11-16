"""Door-to-door journey plans: legs from sources, time arithmetic by code.

Owner test 2026-11-16 (staging): asked for the morning trains that reach a
Paris station by 12:30, Atlas listed direct trains to another terminus next to
a table of "arrivals" with invented connection times, and nothing about getting
from home to the departure station or from the arrival to the place. A trip is
a chain: first mile (starting point to the main departure), main leg or legs,
connections, last mile (arrival to the exact destination); it only works if
every link holds, and the times are computed backwards from the deadline.

The model researches; this tool, offered on travel turns only (``relevant``),
takes the legs it found, each with the page it came from, and code:

- keeps a leg's times only when its source page was read in this turn (D7),
  else marks the leg "not found" with an honest next step;
- computes durations from times, and times from durations, backwards from the
  arrival deadline; computes connection times and flags a missed one;
- computes the straight-line distance from the device position to the first
  stop (geocoded), labelled as straight-line, never turned into a travel time;
- returns the plan as Markdown in the conversation language, and records its
  values as computed for the travel claims check (travel_claims).

No routing engine (OSRM, Valhalla, MOTIS) exists here: legs without a source
stay unknown. Any country, any language; the cues below only decide whether the
tool is offered.
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from packages.validation import describe_validation
from services.orchestrator import travel_claims
from services.orchestrator.location import Position
from services.orchestrator.loop import Message

NAME = "journey_plan"
Mode = Literal[
    "walk", "local_transit", "train", "coach", "flight", "ferry", "car", "bike", "other"
]


class Leg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str = Field(min_length=1, max_length=200)
    destination: str = Field(min_length=1, max_length=200)
    mode: Mode = "other"
    service: str | None = Field(default=None, max_length=80)
    depart: str | None = Field(default=None, max_length=25)
    arrive: str | None = Field(default=None, max_length=25)
    duration_minutes: int | None = Field(default=None, ge=1, le=2880)
    source_url: str | None = Field(default=None, max_length=2048)


class Journey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination: str = Field(min_length=1, max_length=200)
    origin: str | None = Field(default=None, max_length=200)
    arrive_by: str | None = Field(default=None, max_length=25)
    from_device_position: bool = False
    legs: list[Leg] = Field(min_length=1, max_length=8)


DESCRIPTION = (
    "Door-to-door journey plan, computed by code. Call it after your research on "
    "any request about getting from one place to another (by a deadline or not), "
    "before writing the answer. Give the legs in travel order: the first mile from "
    "the person's starting point to the main departure (walk, local transit, car), "
    "the main leg or legs (train, coach, flight, ferry) with each connection, and "
    "the last mile to the exact destination. For each leg give only what a page "
    "you read in this turn states: departure and arrival times (HH:MM or "
    "YYYY-MM-DDTHH:MM) or a duration in minutes, the service (line or train "
    "number), and source_url, the page that states it. Leave a value null when no "
    "page you read gives it: never estimate a time, a duration or a distance "
    "yourself. arrive_by is when the person must be at the destination, local "
    "time; from_device_position is true when the starting point is the position "
    "their device shared. Code checks each leg's source against the pages read "
    "in this turn, works the times backwards from arrive_by, computes connection "
    "times and the straight-line distance from the device position to the first "
    "stop, and returns the plan as Markdown in `rendered`: put `rendered` in your "
    "answer unchanged, then add only what your sources say. A leg marked not "
    "found stays not found: say so and give the next step the plan names."
)


# Added to each search or page result of a trip turn until the plan is made:
# research alone answered with a table of guessed times (owner test 2026-11-16).
NEXT_STEP = (
    "When your research for this trip is done, call journey_plan with the legs in "
    "travel order (null for any value no page you read gives), then answer briefly: "
    "the plan, then at most five short lines."
)
WRITE_NOW = (
    "Research for this trip is closed. Write the answer now from what you read: "
    "the legs you found with their sources, the legs not found said as such, at "
    "most five short lines after any table. Never estimate a time, a duration or "
    "a distance."
)
# Measured on staging 2026-11-15/16 (journal): a model round with tool calls
# p50 1.2 s, p90 3.9 s; a final answer p50 5 s, p90 18 s, but a long trip answer
# took 40 to 80 s; a search 1 to 6 s; the routing call p50 0.9 s, p90 4.3 s
# (8 s on timeout). Research stops after this many results or seconds into the
# turn, so the answer keeps more than half of the 120 s chat deadline.
RESEARCH_RESULTS = 4


def research_seconds() -> float:
    import os

    try:
        value = float(os.environ.get("ATLAS_TRIP_RESEARCH_SECONDS", "40"))
    except ValueError:
        value = 40.0
    return max(10.0, min(80.0, value))


def research_closed(results: int, elapsed: float) -> bool:
    """A trip's research is over: enough results, or its share of the deadline."""
    return results >= RESEARCH_RESULTS or elapsed >= research_seconds()


def declaration() -> Message:
    return {
        "type": "function",
        "function": {
            "name": NAME,
            "description": DESCRIPTION,
            "parameters": Journey.model_json_schema(),
        },
    }


# --- Relevance: is this turn about getting somewhere? ----------------------
# Lower case without accents, five interface languages; only decides whether
# the tool is offered (other turns keep their recorded requests).

_TRANSPORT = re.compile(
    r"\b(?:trains?|tgv|ter|ice|rer|intercites|eurostar|zuge?|bahn|s-bahn|u-bahn|"
    r"tren(?:es)?|treni?|bus|buses|coach|autocar|cars? (?:pour|de|vers)|tram\w*|"
    r"metro|subway|underground|flights?|vols?|flug\w*|vuelos?|voli?|ferr(?:y|ies)|"
    r"fahre|gares?|stations?|bahnhof|hbf|estacion\w*|stazion\w*|airports?|"
    r"aeroports?|flughafen|aeropuertos?|aeroport[oi]|itinerai\w*|itinerar\w*|"
    r"journeys?|trajets?|reise\w*|viajes?|viaggi\w*|correspondances?|connections?|"
    r"umsteig\w*|transbordos?|coincidenz\w*|horaires? de (?:train|bus|car)|"
    r"timetables?|fahrplan\w*|horarios?|orari)\b"
)
_GET_THERE = re.compile(
    r"\b(?:get to|get there|be (?:at|in)|arrive|arrival|reach|"
    r"aller (?:a|au|en|jusqu)|me rendre|etre (?:a|au|en|la)|arriver|rejoindre|"
    r"hinkommen|ankommen|ankunft|sein um|nach \w+ kommen|llegar|estar en|"
    r"arrivare|essere a|raggiungere|andare a)\b"
)
_CLOCK = re.compile(
    r"\b\d{1,2}\s?(?:h|:)\s?\d{2}\b|\b\d{1,2}\s?(?:h|heures?|uhr|am|pm)\b|"
    r"\b(?:midi|noon|mittag|mediodia|mezzogiorno)\b"
)


def _plain(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.lower().replace("’", "'"))
    return "".join(c for c in folded if not unicodedata.combining(c))


def relevant(question: str, previous: str = "", *, located: bool = False) -> bool:
    """A trip between places: offered the journey plan; a short follow-up inherits."""
    text = _plain(question[:4000])
    transport = bool(_TRANSPORT.search(text))
    trip = transport and bool(_CLOCK.search(text) or _GET_THERE.search(text) or located)
    if trip:
        return True
    if previous and len(text) <= 160:
        return relevant(previous, located=located)
    return False


# --- The plan ----------------------------------------------------------------

_TIME = re.compile(
    r"^\s*(?:(\d{4}-\d{2}-\d{2})[T ])?(\d{1,2})\s?[:h]\s?(\d{2})\s*$", re.I
)


class PlanError(ValueError):
    code = "invalid_arguments"


def _time(value: str | None, base: date) -> datetime | None:
    if value is None or not value.strip():
        return None
    found = _TIME.match(value)
    if not found:
        raise PlanError(
            f"time not understood: {value!r}; use HH:MM or YYYY-MM-DDTHH:MM"
        )
    day = date.fromisoformat(found[1]) if found[1] else base
    hours, minutes = int(found[2]), int(found[3])
    if hours > 23 or minutes > 59:
        raise PlanError(f"time out of range: {value!r}")
    return datetime(day.year, day.month, day.day, hours, minutes)


def _same_url(a: str, b: str) -> bool:
    def key(url: str) -> str:
        parts = urlsplit(url.strip())
        return (parts.netloc.lower().removeprefix("www.") + parts.path.rstrip("/")) + (
            "?" + parts.query if parts.query else ""
        )

    return bool(a.strip()) and key(a) == key(b)


WORDS: dict[str, dict[str, Any]] = {
    "en": {
        "title": "Door-to-door plan",
        "headers": ("Leg", "Departure", "Arrival", "Duration", "Source"),
        "modes": {
            "walk": "on foot",
            "local_transit": "local transport",
            "train": "train",
            "coach": "coach",
            "flight": "flight",
            "ferry": "ferry",
            "car": "car",
            "bike": "bike",
            "other": "trip",
        },
        "not_found": "not found",
        "latest": "latest {t}",
        "straight": "straight-line distance {km} km (not a route)",
        "leave": "Leave by {t} at the latest (worked back from arrival at {deadline}).",
        "arrive": "Planned arrival: {t}, for {deadline} requested.",
        "late": "This plan arrives at {t}, after the requested time ({deadline}).",
        "missed": "Missed connection between {a} and {b}: arrival {t1}, departure {t2}.",
        "transfer": "Connection at {place}: {m}.",
        "missing": "Leg not found: {a} → {b}. No page read for this answer gives it. Next step: check it in the local operator's journey planner, or tell me your usual travel time for this leg.",
        "partial": "For the sourced part, leave {place} by {t} at the latest.",
        "from_device": "your position",
    },
    "fr": {
        "title": "Plan porte à porte",
        "headers": ("Étape", "Départ", "Arrivée", "Durée", "Source"),
        "modes": {
            "walk": "à pied",
            "local_transit": "transports locaux",
            "train": "train",
            "coach": "car",
            "flight": "avion",
            "ferry": "ferry",
            "car": "voiture",
            "bike": "vélo",
            "other": "trajet",
        },
        "not_found": "non trouvé",
        "latest": "au plus tard {t}",
        "straight": "distance à vol d’oiseau {km} km (pas un trajet)",
        "leave": "Départ au plus tard à {t} (calculé à rebours depuis l’arrivée à {deadline}).",
        "arrive": "Arrivée prévue : {t}, pour {deadline} demandé.",
        "late": "Ce plan arrive à {t}, après l’heure demandée ({deadline}).",
        "missed": "Correspondance manquée entre {a} et {b} : arrivée {t1}, départ {t2}.",
        "transfer": "Correspondance à {place} : {m}.",
        "missing": "Étape non trouvée : {a} → {b}. Aucune page lue pour cette réponse ne la donne. Prochaine étape : vérifiez-la dans le calculateur d’itinéraire de l’exploitant local, ou donnez-moi votre temps de trajet habituel pour cette étape.",
        "partial": "Pour la partie sourcée, il faut quitter {place} au plus tard à {t}.",
        "from_device": "votre position",
    },
    "de": {
        "title": "Plan von Tür zu Tür",
        "headers": ("Abschnitt", "Abfahrt", "Ankunft", "Dauer", "Quelle"),
        "modes": {
            "walk": "zu Fuß",
            "local_transit": "Nahverkehr",
            "train": "Zug",
            "coach": "Fernbus",
            "flight": "Flug",
            "ferry": "Fähre",
            "car": "Auto",
            "bike": "Fahrrad",
            "other": "Strecke",
        },
        "not_found": "nicht gefunden",
        "latest": "spätestens {t}",
        "straight": "Luftlinie {km} km (keine Route)",
        "leave": "Spätestens um {t} losfahren (rückwärts berechnet ab Ankunft um {deadline}).",
        "arrive": "Geplante Ankunft: {t}, für {deadline} gewünscht.",
        "late": "Dieser Plan kommt um {t} an, nach der gewünschten Zeit ({deadline}).",
        "missed": "Anschluss verpasst zwischen {a} und {b}: Ankunft {t1}, Abfahrt {t2}.",
        "transfer": "Umstieg in {place}: {m}.",
        "missing": "Abschnitt nicht gefunden: {a} → {b}. Keine für diese Antwort gelesene Seite nennt ihn. Nächster Schritt: Prüfen Sie ihn in der Fahrplanauskunft des örtlichen Verkehrsbetriebs, oder nennen Sie mir Ihre übliche Fahrzeit für diesen Abschnitt.",
        "partial": "Für den belegten Teil spätestens um {t} ab {place} losfahren.",
        "from_device": "Ihr Standort",
    },
    "es": {
        "title": "Plan puerta a puerta",
        "headers": ("Tramo", "Salida", "Llegada", "Duración", "Fuente"),
        "modes": {
            "walk": "a pie",
            "local_transit": "transporte local",
            "train": "tren",
            "coach": "autocar",
            "flight": "avión",
            "ferry": "ferry",
            "car": "coche",
            "bike": "bicicleta",
            "other": "trayecto",
        },
        "not_found": "no encontrado",
        "latest": "a más tardar {t}",
        "straight": "distancia en línea recta {km} km (no es una ruta)",
        "leave": "Salir a más tardar a las {t} (calculado hacia atrás desde la llegada a las {deadline}).",
        "arrive": "Llegada prevista: {t}, para las {deadline} pedidas.",
        "late": "Este plan llega a las {t}, después de la hora pedida ({deadline}).",
        "missed": "Conexión perdida entre {a} y {b}: llegada {t1}, salida {t2}.",
        "transfer": "Conexión en {place}: {m}.",
        "missing": "Tramo no encontrado: {a} → {b}. Ninguna página leída para esta respuesta lo da. Siguiente paso: compruébelo en el planificador de viajes del operador local, o dígame su tiempo de trayecto habitual para este tramo.",
        "partial": "Para la parte con fuente, salga de {place} a más tardar a las {t}.",
        "from_device": "su posición",
    },
    "it": {
        "title": "Piano porta a porta",
        "headers": ("Tratta", "Partenza", "Arrivo", "Durata", "Fonte"),
        "modes": {
            "walk": "a piedi",
            "local_transit": "trasporto locale",
            "train": "treno",
            "coach": "pullman",
            "flight": "volo",
            "ferry": "traghetto",
            "car": "auto",
            "bike": "bici",
            "other": "tragitto",
        },
        "not_found": "non trovato",
        "latest": "al più tardi {t}",
        "straight": "distanza in linea d’aria {km} km (non un percorso)",
        "leave": "Partire al più tardi alle {t} (calcolato a ritroso dall’arrivo alle {deadline}).",
        "arrive": "Arrivo previsto: {t}, per le {deadline} richieste.",
        "late": "Questo piano arriva alle {t}, dopo l’ora richiesta ({deadline}).",
        "missed": "Coincidenza persa tra {a} e {b}: arrivo {t1}, partenza {t2}.",
        "transfer": "Coincidenza a {place}: {m}.",
        "missing": "Tratta non trovata: {a} → {b}. Nessuna pagina letta per questa risposta la indica. Prossimo passo: la verifichi nel pianificatore di viaggio dell’operatore locale, o mi dica il suo tempo di viaggio abituale per questa tratta.",
        "partial": "Per la parte con fonte, lasciare {place} al più tardi alle {t}.",
        "from_device": "la sua posizione",
    },
}


def _words(language: str) -> dict[str, Any]:
    return WORDS.get(language, WORDS["en"])


def _clock(value: datetime | None, dated: bool, deadline: datetime | None) -> str:
    if value is None:
        return ""
    text = value.strftime("%H:%M")
    if dated and deadline is not None and value.date() != deadline.date():
        text = value.strftime("%Y-%m-%d ") + text
    return text


def _minutes(value: float) -> str:
    total = int(round(value))
    hours, minutes = divmod(total, 60)
    if hours and minutes:
        return f"{hours} h {minutes:02d} min"
    if hours:
        return f"{hours} h 00 min"
    return f"{minutes} min"


def _cell(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def straight_line_km(origin: Position, latitude: float, longitude: float) -> float:
    """Great-circle distance, one decimal: a straight line, never a route."""
    a, b = math.radians(origin.latitude), math.radians(latitude)
    d = (
        math.sin((b - a) / 2) ** 2
        + math.cos(a)
        * math.cos(b)
        * math.sin(math.radians(longitude - origin.longitude) / 2) ** 2
    )
    return round(6371.0088 * 2 * math.asin(min(1, math.sqrt(d))), 1)


async def geocode_near(place: str, near: Position) -> tuple[float, float] | None:
    """One public map lookup biased around the device position; None if unsure."""
    from services.orchestrator.nearby import OSM, NearbyError
    from packages.privacy import StrictPrivacyUnavailable

    box = (
        f"{near.longitude - 1:.3f},{near.latitude + 1:.3f},"
        f"{near.longitude + 1:.3f},{near.latitude - 1:.3f}"
    )
    try:
        data = await OSM().request(
            "geocode",
            {"q": place, "format": "jsonv2", "limit": "1", "viewbox": box},
        )
        row = data[0]
        return float(row["lat"]), float(row["lon"])
    except (
        NearbyError,
        StrictPrivacyUnavailable,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ):
        return None


def build(
    journey: Journey,
    retrieved: list[str],
    language: str,
    *,
    first_stop_km: float | None = None,
) -> dict[str, Any]:
    """The plan: sourced legs, backward times, connections, rendered Markdown."""
    words = _words(language)
    deadline_value = journey.arrive_by
    probe = _TIME.match(deadline_value or "") if deadline_value else None
    dated = bool(probe and probe[1])
    base = date.fromisoformat(probe[1]) if dated and probe else date(2000, 1, 1)
    deadline = _time(deadline_value, base)
    legs: list[dict[str, Any]] = []
    for leg in journey.legs:
        sourced = bool(leg.source_url) and any(
            _same_url(str(leg.source_url), url) for url in retrieved
        )
        row: dict[str, Any] = {
            "origin": leg.origin,
            "destination": leg.destination,
            "mode": leg.mode,
            "service": leg.service,
            "source_url": leg.source_url if sourced else None,
            "status": "sourced" if sourced else "not_found",
            "depart": None,
            "arrive": None,
            "duration": None,
            "computed": False,
        }
        if leg.source_url and not sourced:
            row["reason"] = "source_not_read_this_turn"
        elif not leg.source_url:
            row["reason"] = "no_source"
        if sourced:
            depart, arrive = _time(leg.depart, base), _time(leg.arrive, base)
            duration = leg.duration_minutes
            if depart and arrive:
                if arrive < depart:
                    arrive += timedelta(days=1)
                duration = int((arrive - depart).total_seconds() // 60)
            elif depart and duration:
                arrive = depart + timedelta(minutes=duration)
            elif arrive and duration:
                depart = arrive - timedelta(minutes=duration)
            row.update(depart=depart, arrive=arrive, duration=duration)
            row["fixed"] = depart is not None and arrive is not None
            if not depart and not arrive and not duration:
                row["status"] = "not_found"
                row["reason"] = "no_time_in_source"
        legs.append(row)
    # Backwards from the deadline: a scheduled leg keeps its times, a leg known
    # by its duration ends when the next one starts (or at the deadline).
    latest: datetime | None = deadline
    problems: list[dict[str, Any]] = []
    for row in reversed(legs):
        if row["status"] != "sourced":
            latest = None
            continue
        if row.get("fixed"):
            latest = row["depart"]
        elif row["duration"] and latest is not None:
            row["arrive"] = latest
            row["depart"] = latest - timedelta(minutes=row["duration"])
            row["computed"] = True
            latest = row["depart"]
        else:
            latest = None
    for earlier, later in zip(legs, legs[1:], strict=False):
        if earlier["arrive"] is None or later["depart"] is None:
            continue
        gap = (later["depart"] - earlier["arrive"]).total_seconds() / 60
        if gap < 0:
            problems.append({"kind": "missed", "after": earlier, "before": later})
        elif later.get("fixed"):
            # Time available at the stop before a scheduled departure.
            earlier["connection"] = gap
    final = legs[-1]["arrive"]
    if deadline is not None and final is not None and final > deadline:
        problems.append({"kind": "late", "arrive": final})
    missing = [row for row in legs if row["status"] != "sourced"]
    verdict = (
        "late"
        if problems
        else "incomplete"
        if missing
        else "on_time"
        if deadline is not None and legs[0]["depart"] is not None
        else "listed"
    )
    # Markdown, in the conversation language.
    start = (
        words["from_device"]
        if journey.from_device_position
        else (journey.origin or legs[0]["origin"])
    )
    headers = words["headers"]
    lines = [
        f"**{words['title']}**",
        "",
        "| " + " | ".join(headers) + " |",
        "|" + "---|" * len(headers),
    ]
    for index, row in enumerate(legs):
        origin = start if index == 0 and journey.from_device_position else row["origin"]
        label = f"{index + 1}. {origin} → {row['destination']} ({words['modes'][row['mode']]}"
        # A line or train of a leg no page gave is not written (route_facts).
        sourced_service = row["service"] and row["status"] == "sourced"
        label += f", {row['service']})" if sourced_service else ")"
        if row["status"] != "sourced":
            depart_text = arrive_text = duration_text = "—"
            source = words["not_found"]
        else:
            depart_text = _clock(row["depart"], dated, deadline) or "—"
            arrive_text = _clock(row["arrive"], dated, deadline) or "—"
            if row["computed"]:
                depart_text = words["latest"].format(t=depart_text)
                arrive_text = words["latest"].format(t=arrive_text)
            duration_text = _minutes(row["duration"]) if row["duration"] else "—"
            host = urlsplit(str(row["source_url"])).netloc.removeprefix("www.")
            source = f"[{host}]({row['source_url']})"
        if index == 0 and first_stop_km is not None:
            kilometres = f"{first_stop_km:.1f}"
            if language != "en":
                kilometres = kilometres.replace(".", ",")
            source += "; " + words["straight"].format(km=kilometres)
        cells = (label, depart_text, arrive_text, duration_text, source)
        lines.append("| " + " | ".join(_cell(str(c)) for c in cells) + " |")
    lines.append("")
    deadline_text = _clock(deadline, dated, None) if deadline else ""
    if dated and deadline is not None:
        deadline_text = deadline.strftime("%Y-%m-%d %H:%M")
    recap: list[str] = []
    for problem in problems:
        if problem["kind"] == "late":
            recap.append(
                words["late"].format(
                    t=_clock(problem["arrive"], dated, deadline), deadline=deadline_text
                )
            )
        else:
            recap.append(
                words["missed"].format(
                    a=problem["after"]["destination"],
                    b=problem["before"]["origin"],
                    t1=_clock(problem["after"]["arrive"], dated, deadline),
                    t2=_clock(problem["before"]["depart"], dated, deadline),
                )
            )
    for row in legs:
        if "connection" in row:
            recap.append(
                words["transfer"].format(
                    place=row["destination"], m=_minutes(row["connection"])
                )
            )
    for row in missing:
        origin = (
            start if row is legs[0] and journey.from_device_position else row["origin"]
        )
        recap.append(words["missing"].format(a=origin, b=row["destination"]))
    if not problems:
        if legs[0]["depart"] is not None and deadline is not None:
            recap.append(
                words["leave"].format(
                    t=_clock(legs[0]["depart"], dated, deadline), deadline=deadline_text
                )
            )
        elif deadline is not None:
            first_known = next(
                (
                    row
                    for row in legs
                    if row["status"] == "sourced"
                    and row["depart"] is not None
                    and all(
                        later["status"] == "sourced"
                        for later in legs[legs.index(row) :]
                    )
                ),
                None,
            )
            if first_known is not None:
                recap.append(
                    words["partial"].format(
                        place=first_known["origin"],
                        t=_clock(first_known["depart"], dated, deadline),
                    )
                )
        if final is not None and deadline is not None and verdict == "on_time":
            recap.append(
                words["arrive"].format(
                    t=_clock(final, dated, deadline), deadline=deadline_text
                )
            )
    lines.extend(f"- {line}" for line in recap)
    rendered = "\n".join(lines).rstrip()

    def plain(row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: (
                value.strftime("%Y-%m-%dT%H:%M") if dated else value.strftime("%H:%M")
            )
            if isinstance(value, datetime)
            else value
            for key, value in row.items()
            if key not in {"fixed", "connection"}
        } | (
            {"connection_minutes": round(row["connection"])}
            if "connection" in row
            else {}
        )

    return {
        "verdict": verdict,
        "legs": [plain(row) for row in legs],
        "straight_line_km_to_first_stop": first_stop_km,
        "rendered": rendered,
        "instruction": "Put `rendered` in the answer unchanged. Do not add any time, "
        "duration or distance that neither `rendered` nor a page you read states.",
    }


async def execute(arguments: str, timeout: float) -> Message:
    turn = travel_claims.current.get()
    try:
        journey = Journey.model_validate_json(arguments)
        first_stop_km: float | None = None
        if (
            journey.from_device_position
            and turn is not None
            and turn.position is not None
        ):
            import asyncio

            try:
                async with asyncio.timeout(max(0.5, min(timeout, 8.0))):
                    found = await geocode_near(
                        journey.legs[0].destination, turn.position
                    )
            except TimeoutError:
                found = None
            if found is not None:
                first_stop_km = straight_line_km(turn.position, *found)
        data = build(
            journey,
            turn.retrieved() if turn is not None else [],
            turn.language if turn is not None else "en",
            first_stop_km=first_stop_km,
        )
    except ValidationError as exc:
        return {
            "error": "invalid_arguments",
            "message": describe_validation(exc),
            "schema": Journey.model_json_schema(),
        }
    except PlanError as exc:
        return {"error": exc.code, "message": str(exc)}
    if turn is not None:
        turn.journey_called = True
        turn.compute(data["rendered"])
    return {"data": data}
