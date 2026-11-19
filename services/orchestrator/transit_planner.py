"""Public transport itineraries computed by a MOTIS 2 journey planner.

Staging -126 (2026-11-18): three trip answers built from web pages still went
wrong where only a timetable knows: a metro taken in the wrong direction (Lyon),
a departure time worked back from a duration the checks had removed (Munich),
and a train that arrived after the person's deadline (Madrid). A journey
planner computes these facts from the operators' published timetables.

Owner decision 2026-11-19: Atlas uses the free public API of Transitous
(https://transitous.org/api/), a MOTIS 2 instance run by volunteers for free and
open-source, non-commercial applications, and any installing organisation can
point ``ATLAS_TRANSIT_URL`` to its own MOTIS 2 server (same API). Their usage
policy is kept by code: a User-Agent naming Atlas, its version and its public
page on every request; a visible link to their sources page and the
OpenStreetMap attribution in every answer that uses a plan; light use (the same
query within ``ATLAS_TRANSIT_CACHE_SECONDS`` is served from a local cache, and
at most ``ATLAS_TRANSIT_DAILY_PLANS`` plan requests a day, well under the 720 a
day their policy calls heavy); the production endpoint only.

The model decides when to ask for a plan (T8) and names the places and the
deadline; code geocodes, plans, keeps only itineraries that leave after now and
arrive by the deadline, converts every time to the local time of its place,
and writes the plan. Its text is recorded as computed facts and as a passage
read in the turn (D7), so the travel checks (travel_claims, route_facts) keep
every line, direction, stop and time the plan gives and leave out any other.
When the planner is disabled, unreachable, over its daily limit or has no
itinerary, the tool says so and the trip is researched on the web as before.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from packages.validation import describe_validation
from services.orchestrator.cache import Cache
from services.orchestrator.loop import Message

NAME = "transit_plan"
DEFAULT_URL = "https://api.transitous.org"
SOURCES_URL = "https://transitous.org/sources/"
OSM_URL = "https://www.openstreetmap.org/copyright"
PUBLIC_PAGE = "https://github.com/leag1234/Atlas-ai-platform"
# Version of this client in the User-Agent when ATLAS_VERSION is not set.
CLIENT_VERSION = "0.7"
# Itineraries written in a plan.
OPTIONS = 3
# Seconds of the search window around the time asked.
SEARCH_WINDOW = 3600
MAX_RESPONSE = 4_000_000
GEOCODE_SECONDS = 86_400


class TransitError(RuntimeError):
    """A planner failure the model reads as an outcome (never a provider error
    in disguise, D5)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Settings:
    url: str
    timeout: float
    daily_plans: int
    cache_seconds: int
    interval: float
    state: Path
    attribution_url: str
    attribution_name: str


def _number(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        value = default
    if value != value:
        value = default
    return max(low, min(high, value))


def settings() -> Settings | None:
    """The planner settings; None when it is off (the web path stays)."""
    if os.environ.get("ATLAS_TRANSIT", "0").strip() != "1":
        return None
    url = os.environ.get("ATLAS_TRANSIT_URL", DEFAULT_URL).strip().rstrip("/")
    if not url.startswith(("https://", "http://")):
        return None
    return Settings(
        url=url,
        timeout=_number("ATLAS_TRANSIT_TIMEOUT", 8, 1, 30),
        daily_plans=int(_number("ATLAS_TRANSIT_DAILY_PLANS", 300, 0, 100_000)),
        cache_seconds=int(_number("ATLAS_TRANSIT_CACHE_SECONDS", 600, 0, 86_400)),
        interval=_number("ATLAS_TRANSIT_MIN_INTERVAL", 0.5, 0, 10),
        state=Path(os.environ.get("ATLAS_TRANSIT_STATE", "BRAIN/transit")),
        attribution_url=os.environ.get(
            "ATLAS_TRANSIT_ATTRIBUTION_URL", SOURCES_URL
        ).strip()
        or SOURCES_URL,
        attribution_name=os.environ.get("ATLAS_TRANSIT_NAME", "Transitous").strip()
        or "Transitous",
    )


def enabled() -> bool:
    return settings() is not None


def user_agent() -> str:
    version = os.environ.get("ATLAS_VERSION", "").strip() or CLIENT_VERSION
    version = re.sub(r"[^\w.+-]", "", version)[:40] or CLIENT_VERSION
    return f"Atlas/{version} (+{PUBLIC_PAGE})"


# --- The client ----------------------------------------------------------------

Fetch = Callable[[str, dict[str, str]], Awaitable[tuple[int, bytes]]]


class Client:
    """Geocode and plan on a MOTIS 2 server, with a cache and a daily cap."""

    def __init__(self, config: Settings, fetch: Fetch | None = None) -> None:
        self.config = config
        self.cache = Cache(config.state / "transit.sqlite")
        self.fetch = fetch or self._fetch
        self.requests = 0

    async def _fetch(self, path: str, params: dict[str, str]) -> tuple[int, bytes]:
        async with aiohttp.ClientSession(
            trust_env=False,
            timeout=aiohttp.ClientTimeout(total=self.config.timeout),
            headers={"User-Agent": user_agent(), "Accept": "application/json"},
        ) as client:
            async with client.get(
                self.config.url + path, params=params, allow_redirects=False
            ) as response:
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE:
                        raise TransitError("planner_response_too_large")
                return response.status, bytes(body)

    async def _spaced(self) -> None:
        """At most one request per ``interval`` seconds across processes."""
        if self.config.interval <= 0:
            return
        self.config.state.mkdir(parents=True, exist_ok=True)
        with (self.config.state / "requests.rate").open("a+") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.05)
            lock.seek(0)
            try:
                previous = float(lock.read() or 0)
            except ValueError:
                previous = 0.0
            await asyncio.sleep(
                max(
                    0.0,
                    min(
                        self.config.interval,
                        previous + self.config.interval - time.time(),
                    ),
                )
            )
            lock.seek(0)
            lock.truncate()
            lock.write(str(time.time()))
            lock.flush()

    def reserve_plan(self) -> bool:
        """Count one plan request against today's cap (UTC day)."""
        day = "transit-plan:" + time.strftime("%Y-%m-%d", time.gmtime())
        with self.cache.connection() as db:
            db.execute("INSERT OR IGNORE INTO quota VALUES (?,0)", (day,))
            cursor = db.execute(
                "UPDATE quota SET used=used+1 WHERE month=? AND used+1<=?",
                (day, self.config.daily_plans),
            )
            return cursor.rowcount == 1

    def plans_today(self) -> int:
        day = "transit-plan:" + time.strftime("%Y-%m-%d", time.gmtime())
        with self.cache.connection() as db:
            row = db.execute("SELECT used FROM quota WHERE month=?", (day,)).fetchone()
        return int(row[0]) if row else 0

    async def get(self, path: str, params: dict[str, str], ttl: int) -> Any:
        from packages.privacy import require_shared

        # The planner receives the places of the trip: refused at a level that
        # keeps request content inside Atlas, before anything is sent.
        require_shared("places_search")
        key = Cache.key({"url": self.config.url, "path": path, "params": params})
        if ttl > 0 and (cached := self.cache.get(key)) is not None:
            return cached["value"]
        if path.endswith("/plan") and not self.reserve_plan():
            raise TransitError("planner_daily_limit")
        await self._spaced()
        self.requests += 1
        try:
            status, body = await self.fetch(path, params)
        except TransitError:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise TransitError("planner_unavailable") from None
        if status == 429:
            raise TransitError("planner_rate_limited", "HTTP 429")
        if status != 200:
            raise TransitError("planner_unavailable", f"HTTP {status}")
        try:
            value = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise TransitError("planner_unavailable", "invalid JSON") from None
        if ttl > 0:
            self.cache.put(key, {"value": value}, ttl)
        return value

    async def geocode(
        self, text: str, language: str, near: tuple[float, float] | None = None
    ) -> list[dict[str, Any]]:
        params = {"text": text[:200], "numResults": "5"}
        if language:
            params["language"] = language
        if near is not None:
            params["place"] = f"{near[0]:.4f},{near[1]:.4f}"
        found = await self.get("/api/v1/geocode", params, GEOCODE_SECONDS)
        return [
            row
            for row in (found if isinstance(found, list) else [])
            if isinstance(row, dict)
            and isinstance(row.get("lat"), (int, float))
            and isinstance(row.get("lon"), (int, float))
        ]

    async def plan(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        when: datetime,
        *,
        arrive_by: bool,
        language: str,
    ) -> dict[str, Any]:
        params = {
            "fromPlace": f"{origin[0]:.5f},{origin[1]:.5f}",
            "toPlace": f"{destination[0]:.5f},{destination[1]:.5f}",
            "time": when.isoformat(timespec="minutes"),
            "arriveBy": "true" if arrive_by else "false",
            "numItineraries": str(OPTIONS),
            "searchWindow": str(SEARCH_WINDOW),
            "detailedLegs": "false",
            "detailedTransfers": "false",
        }
        if language:
            params["language"] = language
        found = await self.get("/api/v6/plan", params, self.config.cache_seconds)
        if not isinstance(found, dict):
            raise TransitError("planner_unavailable", "unexpected answer")
        return found


# --- Places ------------------------------------------------------------------


def _distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    d = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    )
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(d)))


def _area(match: dict[str, Any]) -> str:
    areas = match.get("areas")
    if isinstance(areas, list):
        for area in areas:
            if isinstance(area, dict) and area.get("default") and area.get("name"):
                return str(area["name"])
    return ""


def label(match: dict[str, Any]) -> str:
    name = str(match.get("name") or "").strip()
    area = _area(match)
    return f"{name} ({area})" if area and area.lower() not in name.lower() else name


def choose(matches: list[dict[str, Any]], *, biased: bool) -> dict[str, Any]:
    """The place a name stands for, or ``ambiguous_place`` when two equally good
    matches lie far apart (West 33rd Street in Brooklyn and in Manhattan)."""
    if not matches:
        raise TransitError("place_not_found")
    first = matches[0]
    if not biased:
        for other in matches[1:3]:
            same_score = (
                abs(float(first.get("score") or 0) - float(other.get("score") or 0))
                < 0.01
            )
            far = (
                _distance_km(
                    (float(first["lat"]), float(first["lon"])),
                    (float(other["lat"]), float(other["lon"])),
                )
                > 3
            )
            if same_score and far and label(first) != label(other):
                raise TransitError(
                    "ambiguous_place", "; ".join(label(m) for m in matches[:3])
                )
    return first


def zone_of(name: object, fallback: str = "UTC") -> ZoneInfo:
    for candidate in (name, fallback, "UTC"):
        if isinstance(candidate, str) and candidate:
            try:
                return ZoneInfo(candidate)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return ZoneInfo("UTC")


# --- Itineraries ---------------------------------------------------------------

_RAIL = {
    "RAIL",
    "HIGHSPEED_RAIL",
    "LONG_DISTANCE",
    "NIGHT_RAIL",
    "REGIONAL_FAST_RAIL",
    "REGIONAL_RAIL",
    "SUBURBAN",
}
_KINDS = {
    "WALK": "walk",
    "BIKE": "bike",
    "CAR": "car",
    "BUS": "bus",
    "COACH": "coach",
    "TRAM": "tram",
    "SUBWAY": "metro",
    "METRO": "metro",
    "FERRY": "ferry",
    "AIRPLANE": "flight",
    "FUNICULAR": "funicular",
    "AERIAL_LIFT": "cable_car",
    "AREAL_LIFT": "cable_car",
    "CABLE_CAR": "cable_car",
}


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _line(leg: dict[str, Any]) -> str:
    name = str(
        leg.get("displayName")
        or leg.get("routeShortName")
        or leg.get("tripShortName")
        or ""
    ).strip()
    long_name = str(leg.get("routeLongName") or "").strip()
    if long_name and (not name or name.isdigit()) and len(long_name) <= 40:
        name = f"{long_name} {name}".strip()
    return " ".join(name.split())[:60]


def parse(response: dict[str, Any], zone: ZoneInfo) -> list[dict[str, Any]]:
    """The itineraries of a plan answer: legs with local times and names."""
    itineraries: list[dict[str, Any]] = []
    # ``direct``: a walk (or other street mode) alone, when the places are close.
    rows = [*(response.get("itineraries") or []), *(response.get("direct") or [])]
    for row in rows:
        if not isinstance(row, dict):
            continue
        start, end = _instant(row.get("startTime")), _instant(row.get("endTime"))
        legs_in = row.get("legs")
        if start is None or end is None or not isinstance(legs_in, list):
            continue
        legs: list[dict[str, Any]] = []
        for leg in legs_in:
            if not isinstance(leg, dict):
                continue
            origin = leg.get("from") if isinstance(leg.get("from"), dict) else {}
            target = leg.get("to") if isinstance(leg.get("to"), dict) else {}
            assert isinstance(origin, dict) and isinstance(target, dict)
            depart = _instant(leg.get("scheduledStartTime") or leg.get("startTime"))
            arrive = _instant(leg.get("scheduledEndTime") or leg.get("endTime"))
            if depart is None or arrive is None:
                continue
            mode = str(leg.get("mode") or "OTHER")
            kind = "train" if mode in _RAIL else _KINDS.get(mode, "transit")
            expected = _instant(leg.get("startTime"))
            delay = (
                round((expected - depart).total_seconds() / 60)
                if leg.get("realTime") and expected is not None
                else 0
            )
            legs.append(
                {
                    "kind": kind,
                    "line": "" if kind == "walk" else _line(leg),
                    "headsign": " ".join(str(leg.get("headsign") or "").split())[:80],
                    "operator": " ".join(str(leg.get("agencyName") or "").split())[:60],
                    "from": " ".join(str(origin.get("name") or "").split())[:80],
                    "to": " ".join(str(target.get("name") or "").split())[:80],
                    "platform": str(
                        origin.get("scheduledTrack") or origin.get("track") or ""
                    )[:12],
                    "depart": depart.astimezone(zone_of(origin.get("tz"), zone.key)),
                    "arrive": arrive.astimezone(zone_of(target.get("tz"), zone.key)),
                    "minutes": max(0, round((arrive - depart).total_seconds() / 60)),
                    "delay_minutes": delay,
                    "cancelled": bool(leg.get("cancelled")),
                }
            )
        if not legs or any(leg["cancelled"] for leg in legs):
            continue
        itineraries.append(
            {
                "depart": legs[0]["depart"],
                "arrive": legs[-1]["arrive"],
                # A delay known in real time counts against the deadline.
                "latest": max(legs[-1]["arrive"], end),
                "minutes": max(0, round((end - start).total_seconds() / 60)),
                "transfers": int(row.get("transfers") or 0),
                "legs": legs,
            }
        )
    return itineraries


def selected(
    itineraries: list[dict[str, Any]],
    *,
    now: datetime,
    deadline: datetime | None,
) -> tuple[list[dict[str, Any]], str]:
    """The itineraries to write and the verdict: ``on_time`` (each one leaves
    after now and arrives by the deadline), ``listed`` (no deadline),
    ``none_by_deadline`` or ``no_itinerary``."""
    upcoming = [i for i in itineraries if i["depart"] >= now - timedelta(minutes=1)]
    if upcoming:
        # An itinerary far slower than the fastest one found is not an option
        # (live 2026-11-19: a 9 h 32 min overnight chain next to 4 h 18 min).
        fastest = min(i["minutes"] for i in upcoming)
        upcoming = [i for i in upcoming if i["minutes"] <= fastest * 1.5 + 30]
    if deadline is None:
        upcoming.sort(key=lambda i: i["depart"])
        return upcoming[:OPTIONS], "listed" if upcoming else "no_itinerary"
    kept = [i for i in upcoming if i["latest"] <= deadline]
    if not kept:
        return [], "none_by_deadline" if itineraries else "no_itinerary"
    # The latest departures that still arrive in time, written in time order.
    kept.sort(key=lambda i: (i["depart"], -i["arrive"].timestamp()), reverse=True)
    chosen = sorted(kept[:OPTIONS], key=lambda i: i["depart"])
    return chosen, "on_time"


# --- Wording -----------------------------------------------------------------
# Five interface languages; non-ASCII letters are written as escapes.

WORDS: dict[str, dict[str, Any]] = {
    "en": {
        "title": "Public transport plan",
        "option": "Option {n}",
        "summary": "leave {d}, arrive {a}, {m}, {t}",
        "transfers": ("direct", "1 change", "{n} changes"),
        "walk": "Walk {m} from {a} to {b}",
        "walk_end": "Walk {m} to {b}",
        "ride": "{kind} {line}{towards}: {a} {d} \u2192 {b} {t}",
        "towards": " towards {h}",
        "platform": " (platform {p})",
        "delay": " (expected {m} min late)",
        "deadline": "Arrives {a} for {d} requested.",
        "zones": "Times are local ({z}), from published timetables; check them with the operator before you travel.",
        "none_by_deadline": "No itinerary found that leaves after now and arrives by {d}.",
        "attribution": "Itineraries computed with [{name}]({url}) from the operators' open data. Map data \u00a9 [OpenStreetMap contributors]({osm}).",
        "position": "your position",
        "kinds": {
            "bus": "Bus",
            "coach": "Coach",
            "tram": "Tram",
            "metro": "Metro",
            "train": "Train",
            "ferry": "Ferry",
            "flight": "Flight",
            "funicular": "Funicular",
            "cable_car": "Cable car",
            "transit": "Line",
            "bike": "Bike",
            "car": "Car",
        },
    },
    "fr": {
        "title": "Itin\u00e9raire en transports en commun",
        "option": "Option {n}",
        "summary": "d\u00e9part {d}, arriv\u00e9e {a}, {m}, {t}",
        "transfers": ("direct", "1 correspondance", "{n} correspondances"),
        "walk": "Marche {m} de {a} \u00e0 {b}",
        "walk_end": "Marche {m} jusqu\u2019\u00e0 {b}",
        "ride": "{kind} {line}{towards} : {a} {d} \u2192 {b} {t}",
        "towards": " direction {h}",
        "platform": " (voie {p})",
        "delay": " (retard pr\u00e9vu {m} min)",
        "deadline": "Arriv\u00e9e {a} pour {d} demand\u00e9.",
        "zones": "Heures locales ({z}), d\u2019apr\u00e8s les horaires publi\u00e9s ; v\u00e9rifiez-les aupr\u00e8s de l\u2019exploitant avant de partir.",
        "none_by_deadline": "Aucun itin\u00e9raire trouv\u00e9 qui parte apr\u00e8s maintenant et arrive avant {d}.",
        "attribution": "Itin\u00e9raires calcul\u00e9s avec [{name}]({url}) \u00e0 partir des donn\u00e9es ouvertes des exploitants. Donn\u00e9es cartographiques \u00a9 [contributeurs OpenStreetMap]({osm}).",
        "position": "votre position",
        "kinds": {
            "bus": "Bus",
            "coach": "Car",
            "tram": "Tram",
            "metro": "M\u00e9tro",
            "train": "Train",
            "ferry": "Ferry",
            "flight": "Vol",
            "funicular": "Funiculaire",
            "cable_car": "T\u00e9l\u00e9ph\u00e9rique",
            "transit": "Ligne",
            "bike": "V\u00e9lo",
            "car": "Voiture",
        },
    },
    "de": {
        "title": "Verbindung mit \u00f6ffentlichen Verkehrsmitteln",
        "option": "Option {n}",
        "summary": "ab {d}, an {a}, {m}, {t}",
        "transfers": ("direkt", "1 Umstieg", "{n} Umstiege"),
        "walk": "Fu\u00dfweg {m} von {a} nach {b}",
        "walk_end": "Fu\u00dfweg {m} bis {b}",
        "ride": "{kind} {line}{towards}: {a} {d} \u2192 {b} {t}",
        "towards": " Richtung {h}",
        "platform": " (Gleis {p})",
        "delay": " (voraussichtlich {m} min sp\u00e4ter)",
        "deadline": "Ankunft {a}, gew\u00fcnscht bis {d}.",
        "zones": "Ortszeiten ({z}) laut ver\u00f6ffentlichtem Fahrplan; vor der Fahrt beim Verkehrsunternehmen pr\u00fcfen.",
        "none_by_deadline": "Keine Verbindung gefunden, die nach jetzt abf\u00e4hrt und bis {d} ankommt.",
        "attribution": "Verbindungen berechnet mit [{name}]({url}) aus offenen Daten der Verkehrsunternehmen. Kartendaten \u00a9 [OpenStreetMap-Mitwirkende]({osm}).",
        "position": "Ihr Standort",
        "kinds": {
            "bus": "Bus",
            "coach": "Fernbus",
            "tram": "Tram",
            "metro": "U-Bahn",
            "train": "Zug",
            "ferry": "F\u00e4hre",
            "flight": "Flug",
            "funicular": "Standseilbahn",
            "cable_car": "Seilbahn",
            "transit": "Linie",
            "bike": "Fahrrad",
            "car": "Auto",
        },
    },
    "es": {
        "title": "Itinerario en transporte p\u00fablico",
        "option": "Opci\u00f3n {n}",
        "summary": "salida {d}, llegada {a}, {m}, {t}",
        "transfers": ("directo", "1 transbordo", "{n} transbordos"),
        "walk": "A pie {m} de {a} a {b}",
        "walk_end": "A pie {m} hasta {b}",
        "ride": "{kind} {line}{towards}: {a} {d} \u2192 {b} {t}",
        "towards": " direcci\u00f3n {h}",
        "platform": " (v\u00eda {p})",
        "delay": " (retraso previsto {m} min)",
        "deadline": "Llegada {a} para las {d} pedidas.",
        "zones": "Horas locales ({z}), seg\u00fan los horarios publicados; compru\u00e9belas con el operador antes de salir.",
        "none_by_deadline": "No se encontr\u00f3 ning\u00fan itinerario que salga despu\u00e9s de ahora y llegue antes de las {d}.",
        "attribution": "Itinerarios calculados con [{name}]({url}) a partir de los datos abiertos de los operadores. Datos de mapas \u00a9 [colaboradores de OpenStreetMap]({osm}).",
        "position": "su posici\u00f3n",
        "kinds": {
            "bus": "Autob\u00fas",
            "coach": "Autocar",
            "tram": "Tranv\u00eda",
            "metro": "Metro",
            "train": "Tren",
            "ferry": "Ferry",
            "flight": "Vuelo",
            "funicular": "Funicular",
            "cable_car": "Telef\u00e9rico",
            "transit": "L\u00ednea",
            "bike": "Bicicleta",
            "car": "Coche",
        },
    },
    "it": {
        "title": "Itinerario con i mezzi pubblici",
        "option": "Opzione {n}",
        "summary": "partenza {d}, arrivo {a}, {m}, {t}",
        "transfers": ("diretto", "1 cambio", "{n} cambi"),
        "walk": "A piedi {m} da {a} a {b}",
        "walk_end": "A piedi {m} fino a {b}",
        "ride": "{kind} {line}{towards}: {a} {d} \u2192 {b} {t}",
        "towards": " direzione {h}",
        "platform": " (binario {p})",
        "delay": " (ritardo previsto {m} min)",
        "deadline": "Arrivo {a} per le {d} richieste.",
        "zones": "Ore locali ({z}), secondo gli orari pubblicati; verificarle con l\u2019operatore prima di partire.",
        "none_by_deadline": "Nessun itinerario trovato che parta dopo adesso e arrivi entro le {d}.",
        "attribution": "Itinerari calcolati con [{name}]({url}) dai dati aperti degli operatori. Dati cartografici \u00a9 [collaboratori di OpenStreetMap]({osm}).",
        "position": "la sua posizione",
        "kinds": {
            "bus": "Autobus",
            "coach": "Pullman",
            "tram": "Tram",
            "metro": "Metro",
            "train": "Treno",
            "ferry": "Traghetto",
            "flight": "Volo",
            "funicular": "Funicolare",
            "cable_car": "Funivia",
            "transit": "Linea",
            "bike": "Bici",
            "car": "Auto",
        },
    },
}


def words(language: str) -> dict[str, Any]:
    return WORDS.get(language, WORDS["en"])


def _duration(minutes: int) -> str:
    hours, rest = divmod(max(0, minutes), 60)
    if hours and rest:
        return f"{hours} h {rest:02d} min"
    if hours:
        return f"{hours} h 00 min"
    return f"{rest} min"


def _clock(value: datetime, day: date) -> str:
    text = value.strftime("%H:%M")
    if value.date() != day:
        text = value.strftime("%Y-%m-%d ") + text
    return text


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").replace("*", "").split())


def attribution(language: str, config: Settings) -> str:
    return str(words(language)["attribution"]).format(
        name=config.attribution_name, url=config.attribution_url, osm=OSM_URL
    )


def render(
    itineraries: list[dict[str, Any]],
    *,
    origin: str,
    destination: str,
    deadline: datetime | None,
    language: str,
    config: Settings,
) -> tuple[str, list[str]]:
    """The plan as Markdown, and one passage per itinerary (its legs as
    sentences, each with its line, direction, stops and times)."""
    w = words(language)
    lines = [f"**{w['title']}: {_clean(origin)} \u2192 {_clean(destination)}**", ""]
    passages: list[str] = []
    zones: list[str] = []
    for number, itinerary in enumerate(itineraries, 1):
        day = itinerary["depart"].date()
        count = itinerary["transfers"]
        changes = w["transfers"][min(count, 2)].format(n=count)
        heading = (
            w["option"].format(n=number)
            + " ("
            + itinerary["depart"].strftime("%Y-%m-%d")
            + ")"
        )
        summary = w["summary"].format(
            d=_clock(itinerary["depart"], day),
            a=_clock(itinerary["arrive"], day),
            m=_duration(itinerary["minutes"]),
            t=changes,
        )
        lines.append(f"**{heading}**: {summary}")
        sentences: list[str] = [
            f"{_clean(origin)} \u2192 {_clean(destination)}. {summary}."
        ]
        legs = itinerary["legs"]
        for index, leg in enumerate(legs):
            for moment in (leg["depart"], leg["arrive"]):
                key = str(getattr(moment.tzinfo, "key", "") or "")
                if key and key not in zones:
                    zones.append(key)
            start = origin if leg["from"] == "START" else leg["from"]
            end = destination if leg["to"] == "END" else leg["to"]
            if leg["kind"] == "walk":
                if leg["minutes"] <= 0:
                    continue
                text = (
                    w["walk_end"].format(m=_duration(leg["minutes"]), b=_clean(end))
                    if index == len(legs) - 1 or start == end
                    else w["walk"].format(
                        m=_duration(leg["minutes"]), a=_clean(start), b=_clean(end)
                    )
                )
            else:
                kind = w["kinds"].get(leg["kind"], w["kinds"]["transit"])
                line = _clean(leg["line"])
                if line.lower().startswith(kind.lower() + " "):
                    kind, line = line, ""
                text = w["ride"].format(
                    kind=kind,
                    line=line,
                    towards=w["towards"].format(h=_clean(leg["headsign"]))
                    if leg["headsign"]
                    else "",
                    a=_clean(start),
                    d=_clock(leg["depart"], day),
                    b=_clean(end),
                    t=_clock(leg["arrive"], day),
                )
                text = text.replace("  ", " ")
                if leg["platform"]:
                    text += w["platform"].format(p=_clean(leg["platform"]))
                if leg["delay_minutes"] > 0:
                    text += w["delay"].format(m=leg["delay_minutes"])
            lines.append(f"- {text}")
            sentences.append(text + ".")
        if deadline is not None:
            check = w["deadline"].format(
                a=_clock(itinerary["arrive"], deadline.date()),
                d=_clock(deadline, deadline.date()),
            )
            lines.append(f"- {check}")
            sentences.append(check)
        lines.append("")
        passages.append(" ".join(sentences))
    if not itineraries and deadline is not None:
        lines.append(
            w["none_by_deadline"].format(d=deadline.strftime("%Y-%m-%d %H:%M"))
        )
        lines.append("")
    if zones:
        lines.append(w["zones"].format(z=", ".join(zones)))
    lines.append(attribution(language, config))
    return "\n".join(lines).strip(), passages


# --- The tool ----------------------------------------------------------------


class TransitPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination: str = Field(min_length=2, max_length=200)
    origin: str | None = Field(default=None, max_length=200)
    from_device_position: bool = False
    arrive_by: str | None = Field(default=None, max_length=25)
    depart_at: str | None = Field(default=None, max_length=25)


DESCRIPTION = (
    "Public transport itineraries computed by a journey planner from the "
    "operators' published timetables (trains, metro, tram, bus, coach, ferry, "
    "with the walks between them). Call it first on a request to get from one "
    "place to another by public transport, before any web search. Give the "
    "destination and the origin as the person wrote them (an address, a station, "
    "a district with its city), or from_device_position true when the starting "
    "point is the position their device shared. arrive_by is when the person "
    "must be there, depart_at when they want to leave, as YYYY-MM-DDTHH:MM local "
    "time of that place (from the calendar of the turn for 'tomorrow' or a "
    "weekday); neither means leaving now. Code keeps only the itineraries that "
    "leave after now and arrive by arrive_by, and returns them in `rendered` "
    "with lines, directions, stops, local times and changes: put `rendered` in "
    "your answer unchanged, including its attribution line, then add only what "
    "a page you read says (fares, disruptions). Never add a time, line, "
    "direction or duration of your own. If the outcome is not `planned`, follow "
    "its `next_step`."
)

NEXT_STEPS = {
    "ambiguous_place": "Ask the person which place they mean, or call again with "
    "the district or city added to the name.",
    "place_not_found": "Call again with the place written another way (station "
    "name, street and city), or research the trip on the web.",
    "none_by_deadline": "Say that the planner found no itinerary that arrives in "
    "time, and research earlier or other options on the web; never present a "
    "late itinerary as on time.",
    "origin_missing": "Ask the person where they start from, or use the "
    "position their device shared.",
    "no_itinerary": "The planner has no timetable data for this trip: research "
    "it on the web and use journey_plan.",
}
FALLBACK = (
    "The journey planner is not available for this turn: research the trip on "
    "the web and use journey_plan."
)


def declaration() -> Message:
    return {
        "type": "function",
        "function": {
            "name": NAME,
            "description": DESCRIPTION,
            "parameters": TransitPlan.model_json_schema(),
        },
    }


_LOCAL = re.compile(
    r"^\s*(?:(\d{4}-\d{2}-\d{2})[T ])?(\d{1,2})\s?[:h]\s?(\d{2})\s*$", re.I
)


def local_time(
    value: str | None, zone: ZoneInfo, now: datetime, days: list[str]
) -> datetime | None:
    """``value`` (HH:MM or YYYY-MM-DDTHH:MM) as an aware time of ``zone``; a
    bare time is on the day the person named, else today there."""
    if value is None or not value.strip():
        return None
    found = _LOCAL.match(value)
    if not found:
        raise TransitError("invalid_time", f"{value!r}: use YYYY-MM-DDTHH:MM")
    hours, minutes = int(found[2]), int(found[3])
    if hours > 23 or minutes > 59:
        raise TransitError("invalid_time", f"{value!r} is out of range")
    if found[1]:
        day = date.fromisoformat(found[1])
    elif days:
        day = date.fromisoformat(days[0])
    else:
        day = now.astimezone(zone).date()
    return datetime(day.year, day.month, day.day, hours, minutes, tzinfo=zone)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _point(match: dict[str, Any]) -> tuple[float, float]:
    return float(match["lat"]), float(match["lon"])


async def compute(
    request: TransitPlan,
    *,
    config: Settings,
    language: str,
    position: tuple[float, float] | None,
    home_zone: str,
    days: list[str],
    now: datetime | None = None,
    client: Client | None = None,
) -> dict[str, Any]:
    """The plan for one trip request, or an outcome with its next step."""
    now = now or _now()
    client = client or Client(config)
    w = words(language)
    if request.from_device_position and position is not None:
        start = position
        origin_label = w["position"]
        origin_zone = home_zone
    elif request.origin:
        found = choose(
            await client.geocode(request.origin, language, position),
            biased=position is not None,
        )
        start, origin_label, origin_zone = (
            _point(found),
            label(found),
            str(found.get("tz") or ""),
        )
    else:
        raise TransitError("origin_missing")
    target = choose(
        await client.geocode(request.destination, language, start), biased=False
    )
    zone = zone_of(target.get("tz"), origin_zone or home_zone)
    deadline = local_time(request.arrive_by, zone, now, days)
    leave = local_time(request.depart_at, zone_of(origin_zone, zone.key), now, days)
    when = deadline or leave or now.astimezone(zone)
    response = await client.plan(
        start,
        _point(target),
        when,
        arrive_by=deadline is not None,
        language=language,
    )
    itineraries, verdict = selected(parse(response, zone), now=now, deadline=deadline)
    destination_label = label(target)
    rendered, passages = render(
        itineraries,
        origin=origin_label,
        destination=destination_label,
        deadline=deadline,
        language=language,
        config=config,
    )
    plain = [
        {
            **{k: v for k, v in itinerary.items() if k not in {"legs", "latest"}},
            "depart": itinerary["depart"].isoformat(timespec="minutes"),
            "arrive": itinerary["arrive"].isoformat(timespec="minutes"),
            "legs": [
                {
                    **leg,
                    "depart": leg["depart"].isoformat(timespec="minutes"),
                    "arrive": leg["arrive"].isoformat(timespec="minutes"),
                }
                for leg in itinerary["legs"]
            ],
        }
        for itinerary in itineraries
    ]
    outcome = "planned" if itineraries else verdict
    data: dict[str, Any] = {
        "outcome": outcome,
        "verdict": verdict,
        "origin": origin_label,
        "destination": destination_label,
        "arrive_by": deadline.isoformat(timespec="minutes") if deadline else None,
        "itineraries": plain,
        "source_urls": [config.attribution_url, OSM_URL],
        "attribution": attribution(language, config),
    }
    if itineraries:
        data["rendered"] = rendered
        data["passages"] = passages
        data["instruction"] = (
            "Put `rendered` in the answer unchanged, with its attribution line. "
            "Do not add any time, line, direction or duration it does not state."
        )
    else:
        data["next_step"] = NEXT_STEPS.get(verdict, FALLBACK)
        if verdict == "none_by_deadline":
            data["rendered"] = rendered
    return data


async def execute(arguments: str, timeout: float, home_zone: str = "UTC") -> Message:
    """The tool call: a plan, or an outcome the model reads; facts recorded."""
    from services.orchestrator import grounding, travel_claims

    config = settings()
    if config is None:
        return {"error": "tool_unavailable", "next_step": FALLBACK}
    turn = travel_claims.current.get()
    try:
        request = TransitPlan.model_validate_json(arguments)
    except ValidationError as exc:
        return {
            "error": "invalid_arguments",
            "message": describe_validation(exc),
            "schema": TransitPlan.model_json_schema(),
        }
    from packages.privacy import PrivacyRefusal

    position = (
        (turn.position.latitude, turn.position.longitude)
        if turn is not None and turn.position is not None
        else None
    )
    try:
        async with asyncio.timeout(max(1.0, min(timeout, config.timeout * 3))):
            data = await compute(
                request,
                config=config,
                language=turn.language if turn is not None else "en",
                position=position,
                home_zone=(turn.zone if turn is not None else "") or home_zone,
                days=turn.trip_dates() if turn is not None else [],
            )
    except PrivacyRefusal:
        return {"error": "tool_unavailable", "next_step": FALLBACK}
    except TimeoutError:
        return {"data": {"outcome": "planner_unavailable", "next_step": FALLBACK}}
    except TransitError as exc:
        outcome: dict[str, Any] = {"outcome": exc.code}
        if exc.detail:
            outcome["detail"] = exc.detail
        if exc.code == "planner_daily_limit":
            # A limit stays a limit (D5): said as such, then the web path.
            outcome["message"] = "The journey planner's daily request limit is reached."
        outcome["next_step"] = NEXT_STEPS.get(exc.code, FALLBACK)
        return {"data": outcome}
    if data.get("rendered"):
        # Computed facts of this turn (D7): the plan as written, and each
        # itinerary as a passage read from the planner, so the travel checks
        # keep its lines, directions, stops and times and nothing else.
        if turn is not None:
            turn.compute(str(data["rendered"]))
            for passage in data.get("passages", []):
                turn.collect({"url": config.attribution_url, "text": passage})
        if (check := grounding.current.get()) is not None:
            check.collect({"text": str(data["rendered"])})
    return {"data": data}


def with_attribution(answer: str, computed: list[str], language: str) -> str:
    """An answer that uses a plan keeps the planner's attribution visible
    (Transitous usage policy): added at the end of the body when missing."""
    config = settings()
    if config is None or not answer.strip():
        return answer
    if not any(config.attribution_url in text and OSM_URL in text for text in computed):
        return answer
    body, fold, footer = answer.partition("<details>")
    if config.attribution_url in body and OSM_URL in body:
        return answer
    line = attribution(language, config)
    return body.rstrip() + "\n\n" + line + ("\n\n" + fold + footer if fold else "")
