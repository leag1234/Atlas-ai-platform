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
import unicodedata
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
# The transit modes of a plan (MOTIS 2 names). The server's default, TRANSIT,
# also allows flights, ride sharing and on-demand taxis (live 2026-11-20: a
# point of Geneva airport was planned from Villeurbanne by TGV to Brussels and
# a flight from Charleroi, leaving the day before). Flights only when the
# person asks for them.
TRANSIT_MODES = (
    "TRAM",
    "SUBWAY",
    "FERRY",
    "BUS",
    "COACH",
    "RAIL",
    "FUNICULAR",
    "AERIAL_LIFT",
    "OTHER",
)
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

    async def geocode_reverse(self, point: tuple[float, float]) -> list[dict[str, Any]]:
        """The addresses at ``point`` (their areas name its town)."""
        params = {
            "place": f"{point[0]:.4f},{point[1]:.4f}",
            "type": "ADDRESS",
            "numResults": "1",
        }
        found = await self.get("/api/v1/reverse-geocode", params, GEOCODE_SECONDS)
        return [
            row
            for row in (found if isinstance(found, list) else [])
            if isinstance(row, dict)
        ]

    async def stops_near(self, point: tuple[float, float]) -> list[dict[str, Any]]:
        """The public transport stops closest to ``point``."""
        params = {
            "place": f"{point[0]:.5f},{point[1]:.5f}",
            "type": "STOP",
            "numResults": "3",
        }
        found = await self.get("/api/v1/reverse-geocode", params, GEOCODE_SECONDS)
        return [
            row
            for row in (found if isinstance(found, list) else [])
            if isinstance(row, dict)
            and isinstance(row.get("lat"), (int, float))
            and isinstance(row.get("lon"), (int, float))
        ]

    async def stops_found(
        self, name: str, language: str, near: tuple[float, float]
    ) -> list[dict[str, Any]]:
        """The stops the geocoder finds for ``name`` around ``near``."""
        params = {
            "text": name[:200],
            "type": "STOP",
            "numResults": "5",
            "place": f"{near[0]:.4f},{near[1]:.4f}",
        }
        if language:
            params["language"] = language
        found = await self.get("/api/v1/geocode", params, GEOCODE_SECONDS)
        return [
            row
            for row in (found if isinstance(found, list) else [])
            if isinstance(row, dict)
            and row.get("type") == "STOP"
            and isinstance(row.get("lat"), (int, float))
            and isinstance(row.get("lon"), (int, float))
        ]

    async def stops_named(
        self, name: str, language: str, near: tuple[float, float]
    ) -> list[dict[str, Any]]:
        """The stops whose name holds ``name`` (an airport's terminal stops),
        searched around ``near``."""
        wanted = _folded(name)
        # "aeroport de Geneve" names the stop "Geneve-Aeroport": the words that
        # name the place, in any order, stand for it too.
        named = place_words(name)
        return [
            row
            for row in await self.stops_found(name, language, near)
            if wanted
            and (
                wanted in _folded(str(row.get("name") or ""))
                or bool(named and named <= place_words(str(row.get("name") or "")))
            )
        ]

    async def plan(
        self,
        origin: tuple[float, float] | str,
        destination: tuple[float, float] | str,
        when: datetime,
        *,
        arrive_by: bool,
        language: str,
        flights: bool = False,
        window: int = SEARCH_WINDOW,
    ) -> dict[str, Any]:
        params = {
            "fromPlace": _place(origin),
            "toPlace": _place(destination),
            "time": when.isoformat(timespec="minutes"),
            "arriveBy": "true" if arrive_by else "false",
            "numItineraries": str(OPTIONS),
            "searchWindow": str(window),
            "transitModes": ",".join(
                (*TRANSIT_MODES, "AIRPLANE") if flights else TRANSIT_MODES
            ),
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


def _place(value: tuple[float, float] | str) -> str:
    """A plan end: coordinates, or a stop id as the geocoder returned it."""
    if isinstance(value, str):
        return value
    return f"{value[0]:.5f},{value[1]:.5f}"


def _folded(text: str) -> str:
    """Lower case without accents or punctuation, single spaces."""
    plain = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(
        c if c.isalnum() else " " for c in plain if not unicodedata.combining(c)
    )
    return " ".join(plain.split())


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


# Words that name a transport place, by kind (folded: lower case, no
# accents), in the languages people ask in. A destination named "aeroport de
# Geneve" is the airport's station, never the town around it.
_TRANSPORT_WORDS: dict[str, frozenset[str]] = {
    "airport": frozenset(
        "airport airports aeroport aeroports aeropuerto aeroporto aeroportul "
        "flughafen luchthaven lotnisko letiste letisko flygplats lufthavn "
        "lentoasema repuloter havalimani aerodrome aerodromo airfield".split()
    ),
    "station": frozenset(
        "station stations gare gares bahnhof hauptbahnhof hbf estacion estacao "
        "stazione stacja stanice stanica statie spoorwegstation banegard "
        "jarnvagsstation rautatieasema railway metro subway underground ubahn "
        "sbahn".split()
    ),
    "bus": frozenset(
        "routiere busbahnhof zob autostazione autobuses autobus rodoviaria bus "
        "buses coach autocars".split()
    ),
    # Not "porto" nor "puerto": they name cities (Porto, Puerto Madryn).
    "port": frozenset(
        "port hafen harbour harbor ferry ferries pier wharf maritime "
        "embarcadero".split()
    ),
    # A terminal is any of them (an airport's, a ferry's, a coach's).
    "terminal": frozenset("terminal terminals terminus".split()),
}
# The same in scripts written without spaces (Kyoto's station is "京都駅").
_TRANSPORT_MARKS = {
    "\u7a7a\u6e2f": "airport",  # 空港
    "\u673a\u573a": "airport",  # 机场
    "\u6a5f\u5834": "airport",  # 機場
    "\uacf5\ud56d": "airport",  # 공항
    "\u99c5": "station",  # 駅
    "\u7ad9": "station",  # 站
}
# The kinds of place a stop's modes serve.
_MODE_KINDS = {
    "AIRPLANE": "airport",
    "RAIL": "station",
    "HIGHSPEED_RAIL": "station",
    "LONG_DISTANCE": "station",
    "NIGHT_RAIL": "station",
    "REGIONAL_RAIL": "station",
    "REGIONAL_FAST_RAIL": "station",
    "SUBURBAN": "station",
    "SUBWAY": "station",
    "METRO": "station",
    "BUS": "bus",
    "COACH": "bus",
    "FERRY": "port",
}
# Geocoder categories of a town, an area or a district (MOTIS gives OSM
# place kinds: "town", "place_capital_8").
_AREA_CATEGORIES = frozenset(
    "city town village hamlet suburb neighbourhood neighborhood quarter "
    "borough locality municipality county state province region country "
    "district island isolated_dwelling administrative boundary".split()
)
# How far from the first match a stop of the named place may lie; an
# airport lies outside the town it is named after (Munich's, 28 km).
NAMED_STOP_KM = 10.0
NAMED_AIRPORT_KM = 40.0


def _transport_kinds(name: str) -> set[str]:
    """The kinds of transport place a name names (an airport, a station...)."""
    folded = _folded(name)
    words = set(folded.split())
    kinds = {kind for kind, marks in _TRANSPORT_WORDS.items() if words & marks}
    kinds |= {kind for mark, kind in _TRANSPORT_MARKS.items() if mark in folded}
    if "bus" in kinds:
        # "gare routiere", "bus station": the bus station, not the railway's.
        kinds.discard("station")
    return kinds


def _name_words(name: str) -> set[str]:
    """The words of a name that say which place it is: no transport word."""
    folded = _folded(name)
    for mark in _TRANSPORT_MARKS:
        folded = folded.replace(mark, " ")
    every = set().union(*_TRANSPORT_WORDS.values())
    # Three letters or more, as in place_words; a word of another script
    # ("京都") may be shorter.
    return {
        w
        for w in folded.split()
        if (len(w) >= 3 or not w.isascii()) and w not in every and w not in _GENERIC
    }


def _same_word(a: str, b: str) -> bool:
    # "Geneva" and "Geneve": the same name in two languages.
    return a == b or (len(a) == len(b) >= 5 and a[:-1] == b[:-1])


def _town_or_address(match: dict[str, Any]) -> bool:
    if match.get("type") == "ADDRESS":
        return True
    category = str(match.get("category") or "").casefold()
    return match.get("type") == "PLACE" and (
        category in _AREA_CATEGORIES or category.startswith("place_")
    )


# Geocoder categories of a transport place mapped as an area (an airport's
# aerodrome, amid its runways), by kind.
_FEATURE_CATEGORIES = {
    "aerodrome": "airport",
    "airport": "airport",
    "railway_station": "station",
    "train_station": "station",
    "bus_station": "bus",
    "ferry_terminal": "port",
}


def _feature_kind(match: dict[str, Any], flights: bool) -> str:
    """The kind of transport place a match is when no plan should end at it:
    an aerodrome or a station area (its point lies amid the runways or the
    tracks), or an airport's own flight stop when the person does not fly."""
    if match.get("type") == "STOP":
        return "" if _served(match, flights) else "airport"
    category = re.sub(r"_\d+$", "", str(match.get("category") or "").casefold())
    return _FEATURE_CATEGORIES.get(category, "")


def _named_terms(
    named: str, matches: list[dict[str, Any]], flights: bool
) -> tuple[set[str], set[str]] | None:
    """The kinds of transport place and the words a stop must carry to stand
    for the first match, or None when the first match is the place asked.

    The first match gives way to a stop when it is a town, an area or an
    address (the name asked holds a transport word the town's own name does
    not: "Port Melbourne" is the town), the transport place itself as the
    geocoder maps it (an aerodrome, a flight stop: its own words then name
    the stop too), or a place of another kind whose name says more than the
    name asked (the car rental desk "Easirent London Heathrow Airport LHR"
    for "London Heathrow Airport (LHR), UK"). A place whose every word is a
    word of the name asked ("Mercure Sydney Airport") is the place asked.
    """
    if not named or not matches:
        return None
    first = matches[0]
    first_name = str(first.get("name") or "")
    asked = _transport_kinds(named)
    wanted = _name_words(named)
    if feature := _feature_kind(first, flights):
        asked.add(feature)
        wanted |= _name_words(first_name)
    elif _town_or_address(first):
        asked -= _transport_kinds(first_name)
    elif first.get("type") == "STOP":
        return None
    else:
        own = _name_words(first_name)
        if not own or all(any(_same_word(w, q) for q in wanted) for w in own):
            return None
    if not asked or not wanted:
        return None
    return asked, wanted


def core_name(named: str) -> str:
    """A place name without its qualifiers: "Aeroport de Geneve (GVA), Le
    Grand-Saconnex, Suisse" is "Aeroport de Geneve"."""
    core = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", named).split(",")[0]
    return " ".join(core.split()) or " ".join(named.split())


def named_stop(
    named: str, matches: list[dict[str, Any]], *, flights: bool = False
) -> dict[str, Any] | None:
    """The stop of the transport place a name names when the geocoder put
    something else first (``_named_terms``); None to keep the first match.

    Staging -135: "Aeroport de Geneve (GVA), Le Grand-Saconnex, Suisse" put
    the town Le Grand-Saconnex first and the station Geneve-Aeroport fourth,
    and the plans ended in the town. Staging -138: "Aeroport de Geneve (GVA),
    Geneve, Suisse" put the aerodrome first (its point amid the runways, a
    15-minute walk from a bus stop) and the station second. A stop among the
    results fetched stands for the place when its name carries a transport
    word of the kind named (or its modes serve that kind), every other word
    of its name is a word of the name asked (any order, accents folded;
    qualifiers such as a code, a commune or a country in the name asked do
    not matter), and it lies within NAMED_STOP_KM of the first match
    (NAMED_AIRPORT_KM for an airport). Of several, the one that covers the
    most words of the name, then the geocoder's order.
    """
    terms = _named_terms(named, matches, flights)
    if terms is None:
        return None
    return _stop_for(matches[0], matches[:5], *terms, flights=flights)


def _stop_for(
    first: dict[str, Any],
    candidates: list[dict[str, Any]],
    asked: set[str],
    wanted: set[str],
    *,
    flights: bool,
) -> dict[str, Any] | None:
    found: list[tuple[int, bool, int, dict[str, Any]]] = []
    for index, match in enumerate(candidates):
        if match.get("type") != "STOP" or not _served(match, flights):
            continue
        name = str(match.get("name") or "")
        words = _name_words(name)
        if not words or not all(any(_same_word(w, q) for q in wanted) for w in words):
            continue
        by_name = bool(asked & _transport_kinds(name)) or (
            "terminal" in asked and bool(_transport_kinds(name))
        )
        modes = match.get("modes")
        served = (
            {_MODE_KINDS.get(str(m), "") for m in modes}
            if isinstance(modes, list)
            else set()
        )
        by_modes = bool(asked & served) or ("terminal" in asked and bool(served - {""}))
        if not (by_name or by_modes):
            continue
        reach = NAMED_AIRPORT_KM if "airport" in asked else NAMED_STOP_KM
        if _distance_km(_point(first), _point(match)) > reach:
            continue
        found.append((len(words), by_name, index, match))
    if not found:
        return None
    found.sort(key=lambda row: (-row[0], not row[1], row[2]))
    return found[0][3]


def choose(
    matches: list[dict[str, Any]],
    *,
    biased: bool,
    named: str = "",
    flights: bool = False,
) -> dict[str, Any]:
    """The place a name stands for, or ``ambiguous_place`` when two equally good
    matches lie far apart (West 33rd Street in Brooklyn and in Manhattan). The
    station or airport a name names stands for it over the town around it
    (``named_stop``)."""
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
    stop = named_stop(named, matches, flights=flights) if named else None
    return stop or first


def zone_at(latitude: float, longitude: float) -> str:
    """The time zone at a point (offline, from the zone boundaries the
    opening-hours library ships); "" when unknown or a sea zone."""
    try:
        from opening_hours import OpeningHours

        now = datetime.now(ZoneInfo("UTC"))
        hours = OpeningHours("24/7", coords=(latitude, longitude))
        start = next(hours.intervals(now, now + timedelta(minutes=1)))[0]
        name = str(start.tzinfo or "")
        ZoneInfo(name)
    except Exception:  # noqa: BLE001 - an unknown zone leaves the person's own
        return ""
    return "" if name.startswith("Etc/") else name


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


# Kana to Latin letters (modified Hepburn), for service and stop names a
# planner gives only in Japanese script (live 2026-11-19: "\u306e\u305e\u307f11\u53f7" in an
# English answer). Katakana share the table, 0x60 code points apart.
_KANA_ROWS = (
    ("\u3042\u3044\u3046\u3048\u304a", ("a", "i", "u", "e", "o")),
    ("\u304b\u304d\u304f\u3051\u3053", ("ka", "ki", "ku", "ke", "ko")),
    ("\u304c\u304e\u3050\u3052\u3054", ("ga", "gi", "gu", "ge", "go")),
    ("\u3055\u3057\u3059\u305b\u305d", ("sa", "shi", "su", "se", "so")),
    ("\u3056\u3058\u305a\u305c\u305e", ("za", "ji", "zu", "ze", "zo")),
    ("\u305f\u3061\u3064\u3066\u3068", ("ta", "chi", "tsu", "te", "to")),
    ("\u3060\u3062\u3065\u3067\u3069", ("da", "ji", "zu", "de", "do")),
    ("\u306a\u306b\u306c\u306d\u306e", ("na", "ni", "nu", "ne", "no")),
    ("\u306f\u3072\u3075\u3078\u307b", ("ha", "hi", "fu", "he", "ho")),
    ("\u3070\u3073\u3076\u3079\u307c", ("ba", "bi", "bu", "be", "bo")),
    ("\u3071\u3074\u3077\u307a\u307d", ("pa", "pi", "pu", "pe", "po")),
    ("\u307e\u307f\u3080\u3081\u3082", ("ma", "mi", "mu", "me", "mo")),
    ("\u3089\u308a\u308b\u308c\u308d", ("ra", "ri", "ru", "re", "ro")),
    ("\u3084\u3086\u3088\u308f\u3092\u3093", ("ya", "yu", "yo", "wa", "o", "n")),
    ("\u3041\u3043\u3045\u3047\u3049", ("a", "i", "u", "e", "o")),
)
_KANA = {kana: latin for row, sounds in _KANA_ROWS for kana, latin in zip(row, sounds)}
_SMALL_Y = {"\u3083": "a", "\u3085": "u", "\u3087": "o"}
_SOKUON = "\u3063"


def _hiragana(character: str) -> str:
    code = ord(character)
    return chr(code - 0x60) if 0x30A1 <= code <= 0x30F6 else character


def romanised(text: str) -> str:
    """Kana written in Latin letters; any other character kept as it is."""
    out: list[str] = []
    double = False
    for raw in text:
        character = _hiragana(raw)
        if character == _SOKUON:
            double = True
            continue
        if character in _SMALL_Y and out and out[-1].endswith("i"):
            base = out[-1][:-1]
            vowel = _SMALL_Y[character]
            out[-1] = base + vowel if base in {"sh", "ch", "j"} else base + "y" + vowel
            continue
        if character == "\u30fc":  # long vowel mark
            continue
        latin = _KANA.get(character, _SMALL_Y.get(character, raw))
        if double and latin[:1].isalpha() and latin[:1] not in "aeiou":
            latin = ("t" if latin.startswith("ch") else latin[0]) + latin
        double = False
        out.append(latin)
    return "".join(out)


def _latin(text: str) -> bool:
    return all(
        not c.isalpha() or unicodedata.name(c, "").startswith("LATIN") for c in text
    )


def service_name(name: str) -> str:
    """A line name readable in a Latin-script answer: romanised, the original
    kept in parentheses ("Nozomi 11 (\u306e\u305e\u307f11\u53f7)")."""
    if not name or _latin(name):
        return name
    latin = romanised(name)
    kept = "".join(c if _latin(c) else " " for c in latin)
    kept = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", kept)
    words = [w[:1].upper() + w[1:] for w in kept.split()]
    if not any(c.isalpha() for c in "".join(words)):
        return name
    return f"{' '.join(words)} ({name})"


_ENDPOINTS = frozenset({"START", "END"})


def place_name(name: str) -> str:
    """A stop name as people write it: "TOKYO" becomes "Tokyo"; a name in
    kana only is romanised, the original kept in parentheses."""
    if name in _ENDPOINTS:
        # The planner's own markers for the origin and destination places.
        return name
    letters = [c for c in name if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and _latin(name):
        if any(len(w) >= 4 and w.isalpha() for w in re.split(r"[\s/().,-]+", name)):
            return re.sub(r"[^\W\d_]+", lambda m: m[0][:1] + m[0][1:].lower(), name)
        return name
    if not _latin(name):
        latin = romanised(name)
        if _latin(latin):
            return f"{latin[:1].upper()}{latin[1:]} ({name})"
    return name


def parse(
    response: dict[str, Any], zone: ZoneInfo, *, flights: bool = False
) -> list[dict[str, Any]]:
    """The itineraries of a plan answer: legs with local times and names. An
    itinerary with a flight is left out unless the person asked to fly (a
    server that ignores ``transitModes`` still plans no flight)."""
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
                    "line": "" if kind == "walk" else service_name(_line(leg)),
                    "headsign": place_name(
                        " ".join(str(leg.get("headsign") or "").split())[:80]
                    ),
                    "operator": " ".join(str(leg.get("agencyName") or "").split())[:60],
                    "from": place_name(
                        " ".join(str(origin.get("name") or "").split())[:80]
                    ),
                    "to": place_name(
                        " ".join(str(target.get("name") or "").split())[:80]
                    ),
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
        if not flights and any(leg["kind"] == "flight" for leg in legs):
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


# A plan whose best itinerary is far slower than travel over that distance
# usually is, or leaves in the night for a morning arrival, is a sign that the
# open data lacks a service (live 2026-11-19: Villeurbanne to Geneva airport,
# 115 km, night buses and a coach via Annecy, no regional train in the data).
DOUBT_KM = 50
DOUBT_KMH = 35
NIGHT_END = 5
# When the itineraries that arrive in time are implausible, the second search
# asks for arrivals up to this many minutes after the deadline (live
# 2026-11-20: for 09:00 at Geneva airport only night coaches arrive in time;
# the first regional train from Lyon, TER 96500 at 06:38, arrives 09:05 with
# its last leg, and was never shown).
LATE_MINUTES = 30
# Without a deadline, the second search looks over a wider window of
# departures for a reasonable itinerary leaving later.
WIDE_WINDOW = 4 * SEARCH_WINDOW


def _slow(itinerary: dict[str, Any], distance_km: float) -> bool:
    """Far slower than travel over that distance usually is."""
    hours = max(itinerary["minutes"], 1) / 60
    return distance_km >= DOUBT_KM and distance_km / hours < DOUBT_KMH


def _at_night(itinerary: dict[str, Any], asked: datetime | None) -> bool:
    """Leaves in the night (or the day before) for a daytime time asked."""
    if asked is None or asked.hour < NIGHT_END + 1:
        return False
    depart = itinerary["depart"]
    return bool(depart.hour < NIGHT_END or depart.date() < asked.date())


def doubts(
    itineraries: list[dict[str, Any]],
    distance_km: float,
    asked: datetime | None,
) -> dict[str, Any]:
    """Why the best itinerary found is implausible ({} when it is not)."""
    if not itineraries:
        return {}
    fastest = min(itineraries, key=lambda i: i["minutes"])
    found: dict[str, Any] = {}
    if _slow(fastest, distance_km):
        hours = max(fastest["minutes"], 1) / 60
        found["slow"] = {
            "km": round(distance_km),
            "minutes": fastest["minutes"],
            "kmh": round(distance_km / hours),
        }
    if all(_at_night(i, asked) for i in itineraries):
        found["night"] = {"depart": fastest["depart"].strftime("%H:%M")}
    return found


# --- Wording -----------------------------------------------------------------
# Five interface languages; non-ASCII letters are written as escapes.

WORDS: dict[str, dict[str, Any]] = {
    "en": {
        "fallback_title": "Fallback only, computed from the open timetable data",
        "snapped": "The planner reaches no stop from {place} itself: the plan starts at {stop}, the nearest stop ({km} km in a straight line).",
        "snapped_end": "The planner reaches no stop at {place} itself: the plan ends at {stop}, the nearest stop found ({km} km in a straight line).",
        "no_connection": "No reasonable public transport connection was found in the open timetable data for this trip: {why}. No page read for this answer confirms a direct service either: check the operators' timetables.",
        "pages": "Pages read about the direct services: {links}.",
        "direct_query": "{a} {b} direct train or coach timetable",
        "doubt": "Caution: {why}. The open timetable data may lack a direct service on this route (for example a cross-border train): check the operators' timetables before you rely on this plan.",
        "doubt_slow": "the fastest itinerary found takes {m} for {km} km",
        "doubt_night": "every itinerary found leaves at night (from {t})",
        "late_doubt": "No reasonable itinerary found arrives by {d}: {why}. The options below are the first reasonable ones found; they arrive after {d}.",
        "late_empty": "No itinerary found arrives by {d}. The options below are the first ones found; they arrive after {d}.",
        "late": "Arrives {a}, {m} after the {d} requested.",
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
        "fallback_title": "Solution de repli seulement, calculée sur les données horaires ouvertes",
        "snapped": "Le calculateur n’atteint aucun arrêt depuis {place} même : l’itinéraire part de {stop}, l’arrêt le plus proche ({km} km à vol d’oiseau).",
        "snapped_end": "Le calculateur n\u2019atteint aucun arr\u00eat \u00e0 {place} m\u00eame : l\u2019itin\u00e9raire arrive \u00e0 {stop}, l\u2019arr\u00eat le plus proche trouv\u00e9 ({km} km \u00e0 vol d\u2019oiseau).",
        "no_connection": "Aucune liaison raisonnable en transports en commun n’a été trouvée dans les données horaires ouvertes pour ce trajet : {why}. Aucune page consultée pour cette réponse ne confirme non plus de liaison directe : vérifiez les horaires des exploitants.",
        "pages": "Pages consultées sur les liaisons directes : {links}.",
        "direct_query": "{a} {b} train ou car direct horaires",
        "doubt": "Attention : {why}. Les donn\u00e9es horaires ouvertes n\u2019ont peut-\u00eatre pas de liaison directe sur ce trajet (par exemple un train transfrontalier) : v\u00e9rifiez les horaires des exploitants avant de vous fier \u00e0 ce plan.",
        "doubt_slow": "l\u2019itin\u00e9raire le plus rapide trouv\u00e9 met {m} pour {km} km",
        "doubt_night": "tous les itin\u00e9raires trouv\u00e9s partent la nuit (d\u00e8s {t})",
        "late_doubt": "Aucun itin\u00e9raire raisonnable trouv\u00e9 n\u2019arrive au plus tard \u00e0 {d} : {why}. Les options ci-dessous sont les premi\u00e8res raisonnables trouv\u00e9es ; elles arrivent apr\u00e8s {d}.",
        "late_empty": "Aucun itin\u00e9raire trouv\u00e9 n\u2019arrive au plus tard \u00e0 {d}. Les options ci-dessous sont les premi\u00e8res trouv\u00e9es ; elles arrivent apr\u00e8s {d}.",
        "late": "Arriv\u00e9e {a}, {m} apr\u00e8s l\u2019heure demand\u00e9e ({d}).",
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
        "fallback_title": "Nur als Notlösung, berechnet aus den offenen Fahrplandaten",
        "snapped": "Von {place} selbst erreicht der Planer keine Haltestelle: Die Verbindung beginnt an {stop}, der nächsten Haltestelle ({km} km Luftlinie).",
        "snapped_end": "An {place} selbst erreicht der Planer keine Haltestelle: Die Verbindung endet an {stop}, der n\u00e4chsten gefundenen Haltestelle ({km} km Luftlinie).",
        "no_connection": "In den offenen Fahrplandaten wurde für diese Reise keine sinnvolle Verbindung mit öffentlichen Verkehrsmitteln gefunden: {why}. Auch keine für diese Antwort gelesene Seite bestätigt eine direkte Verbindung: Prüfen Sie die Fahrpläne der Verkehrsunternehmen.",
        "pages": "Gelesene Seiten zu direkten Verbindungen: {links}.",
        "direct_query": "{a} {b} direkter Zug oder Fernbus Fahrplan",
        "doubt": "Achtung: {why}. In den offenen Fahrplandaten fehlt auf dieser Strecke vielleicht eine direkte Verbindung (zum Beispiel ein grenz\u00fcberschreitender Zug): pr\u00fcfen Sie die Fahrpl\u00e4ne der Verkehrsunternehmen, bevor Sie sich auf diesen Plan verlassen.",
        "doubt_slow": "die schnellste gefundene Verbindung braucht {m} f\u00fcr {km} km",
        "doubt_night": "alle gefundenen Verbindungen fahren nachts ab (ab {t})",
        "late_doubt": "Keine sinnvolle gefundene Verbindung kommt bis {d} an: {why}. Die folgenden Optionen sind die ersten sinnvollen gefundenen; sie kommen nach {d} an.",
        "late_empty": "Keine gefundene Verbindung kommt bis {d} an. Die folgenden Optionen sind die ersten gefundenen; sie kommen nach {d} an.",
        "late": "Ankunft {a}, {m} nach der gew\u00fcnschten Zeit ({d}).",
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
        "fallback_title": "Solo como alternativa, calculado con los datos horarios abiertos",
        "snapped": "El planificador no alcanza ninguna parada desde {place}: el itinerario sale de {stop}, la parada más cercana ({km} km en línea recta).",
        "snapped_end": "El planificador no alcanza ninguna parada en {place}: el itinerario llega a {stop}, la parada m\u00e1s cercana encontrada ({km} km en l\u00ednea recta).",
        "no_connection": "No se encontró ninguna conexión razonable en transporte público en los datos horarios abiertos para este trayecto: {why}. Ninguna página consultada para esta respuesta confirma tampoco un servicio directo: compruebe los horarios de los operadores.",
        "pages": "Páginas consultadas sobre los servicios directos: {links}.",
        "direct_query": "{a} {b} tren o autobús directo horarios",
        "doubt": "Atenci\u00f3n: {why}. Puede que los datos horarios abiertos no tengan un servicio directo en este trayecto (por ejemplo un tren transfronterizo): compruebe los horarios de los operadores antes de fiarse de este plan.",
        "doubt_slow": "el itinerario m\u00e1s r\u00e1pido encontrado tarda {m} para {km} km",
        "doubt_night": "todos los itinerarios encontrados salen de noche (desde las {t})",
        "late_doubt": "Ning\u00fan itinerario razonable encontrado llega antes de las {d}: {why}. Las opciones siguientes son las primeras razonables encontradas; llegan despu\u00e9s de las {d}.",
        "late_empty": "Ning\u00fan itinerario encontrado llega antes de las {d}. Las opciones siguientes son las primeras encontradas; llegan despu\u00e9s de las {d}.",
        "late": "Llegada {a}, {m} despu\u00e9s de la hora pedida ({d}).",
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
        "fallback_title": "Solo come ripiego, calcolato dai dati orari aperti",
        "snapped": "Il pianificatore non raggiunge alcuna fermata da {place}: l’itinerario parte da {stop}, la fermata più vicina ({km} km in linea d’aria).",
        "snapped_end": "Il pianificatore non raggiunge alcuna fermata a {place}: l\u2019itinerario arriva a {stop}, la fermata pi\u00f9 vicina trovata ({km} km in linea d\u2019aria).",
        "no_connection": "Nei dati orari aperti non è stato trovato alcun collegamento ragionevole con i mezzi pubblici per questo tragitto: {why}. Nessuna pagina consultata per questa risposta conferma neppure un servizio diretto: verifichi gli orari degli operatori.",
        "pages": "Pagine consultate sui servizi diretti: {links}.",
        "direct_query": "{a} {b} treno o pullman diretto orari",
        "doubt": "Attenzione: {why}. I dati orari aperti forse non hanno un servizio diretto su questo tragitto (per esempio un treno transfrontaliero): verifichi gli orari degli operatori prima di affidarsi a questo piano.",
        "doubt_slow": "l\u2019itinerario pi\u00f9 veloce trovato impiega {m} per {km} km",
        "doubt_night": "tutti gli itinerari trovati partono di notte (dalle {t})",
        "late_doubt": "Nessun itinerario ragionevole trovato arriva entro le {d}: {why}. Le opzioni seguenti sono le prime ragionevoli trovate; arrivano dopo le {d}.",
        "late_empty": "Nessun itinerario trovato arriva entro le {d}. Le opzioni seguenti sono le prime trovate; arrivano dopo le {d}.",
        "late": "Arrivo {a}, {m} dopo l\u2019ora richiesta ({d}).",
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


def doubt_reason(doubt: dict[str, Any], language: str) -> str:
    """Why a plan is doubtful, in words ("every itinerary found leaves at
    night (from 03:04)")."""
    w = words(language)
    why = []
    if "slow" in doubt:
        why.append(
            w["doubt_slow"].format(
                m=_duration(doubt["slow"]["minutes"]), km=doubt["slow"]["km"]
            )
        )
    if "night" in doubt:
        why.append(w["doubt_night"].format(t=doubt["night"]["depart"]))
    return "; ".join(why)


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
    doubt: dict[str, Any] | None = None,
    snapped: str = "",
    note: str = "",
) -> tuple[str, list[str]]:
    """The plan as Markdown, and one passage per itinerary (its legs as
    sentences, each with its line, direction, stops and times). ``note`` is a
    paragraph about the plan as a whole (its options arrive late)."""
    w = words(language)
    # A doubtful plan is written as a fallback, never as the trip's answer
    # (staging -129: a night chain via Annecy was given as the way to Geneva).
    title = w["fallback_title"] if doubt else w["title"]
    lines = [f"**{title}: {_clean(origin)} \u2192 {_clean(destination)}**", ""]
    if doubt:
        caveat = w["doubt"].format(why=doubt_reason(doubt, language))
        lines[1:1] = ["", f"_{caveat}_"]
    for paragraph in (snapped, note):
        if paragraph:
            lines[-1:-1] = ["", paragraph]
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
            operator = _clean(leg.get("operator") or "")
            if operator and operator.lower() not in text.lower():
                # The operator is in the tool's data: the passage names it, so
                # "Amtrak Acela 2150" is a fact of the plan (staging -128).
                sentences.append(text + f" ({operator}).")
            else:
                sentences.append(text + ".")
        if deadline is not None and itinerary["arrive"] > deadline:
            # Only the second search's options arrive late, and say so.
            check = w["late"].format(
                a=_clock(itinerary["arrive"], deadline.date()),
                m=_duration(
                    round((itinerary["arrive"] - deadline).total_seconds() / 60)
                ),
                d=_clock(deadline, deadline.date()),
            )
            lines.append(f"- {check}")
            sentences.append(check)
        elif deadline is not None:
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
    flights: bool = False


DESCRIPTION = (
    "Public transport itineraries computed by a journey planner from the "
    "operators' published timetables (trains, metro, tram, bus, coach, ferry, "
    "with the walks between them). Call it first on a request to get from one "
    "place to another by public transport, before any web search. Set flights "
    "true only when the person asks to fly; otherwise a plan never includes a "
    "flight. Give the "
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
    "direction or duration of your own. Write to the person in the second "
    "person of their language. If the outcome is not `planned`, follow its "
    "`next_step`."
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
    "planned_late": "No reasonable itinerary arrives by `arrive_by` (see "
    "`on_time_doubt`): say so first, then put `rendered` unchanged. Its options "
    "are the first reasonable ones found; each says how late it arrives. Never "
    "present them as on time.",
    "doubtful": "This plan looks implausible (see `doubts`): research the "
    "operators' direct services for this trip on the web and give them first, "
    "with their sources; give the computed plan only as a fallback, with its "
    "caution line, never as the answer.",
    "doubtful_read": "This plan looks implausible (see `doubts`). Code searched "
    "the web for the operators' direct services and read the pages in "
    "`direct_services`. Open the answer with the direct services these pages "
    "give for this trip (operator, from where, times for the day when they give "
    "them), each with its page link. Then put `rendered` unchanged: it is the "
    "computed plan, titled as a fallback. If these pages give no direct service "
    "for this trip, say first that no reasonable connection was found in the "
    "open data, then put `rendered`. Never present the computed plan as the way "
    "to make this trip.",
    "doubtful_unread": "This plan looks implausible (see `doubts`) and no page "
    "about the operators' direct services could be read. Say first that no "
    "reasonable connection was found in the open data and that the operators' "
    "timetables are to be checked, then put `rendered` unchanged: it is the "
    "computed plan, titled as a fallback. Never present it as the way to make "
    "this trip.",
}
# The pages code reads about the direct services of a doubtful trip: enough to
# find a regional train or a coach, few enough for the turn's deadline.
DIRECT_PAGES = 2
DIRECT_CHARACTERS = 3000
# A stop this close to a place the planner cannot reach stands for it.
SNAP_KM = 1.5
# A stop bearing the place's own name this close stands for it too: an
# airport's point lies amid its runways, its terminal stops 1.7 to 2.5 km away
# (staging -131: Sydney Airport, no stop within reach of its point).
NAMED_KM = 3.0
INSTRUCTION = (
    "Put `rendered` in the answer unchanged, with its attribution line. Do not "
    "add any time, line, direction or duration it does not state. The trip is "
    "on `day`: write that date. Write to the person in the second person of "
    "their language, never in their own first-person words ('which train do I "
    "take' is answered 'the train for you is', not 'the train I take is')."
)
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
    if found[1] and (not days or found[1] in days):
        day = date.fromisoformat(found[1])
    elif days:
        # The day the person named wins over another the call gives (staging
        # -128: "tmrw" planned for today, so every train arrived too late).
        day = date.fromisoformat(days[0])
    else:
        day = now.astimezone(zone).date()
    return datetime(day.year, day.month, day.day, hours, minutes, tzinfo=zone)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _point(match: dict[str, Any]) -> tuple[float, float]:
    return float(match["lat"]), float(match["lon"])


def _end(
    chosen: dict[str, Any], matches: list[dict[str, Any]]
) -> tuple[float, float] | str:
    """A plan end: the stop id of the station or airport the person named when
    it stood for the town the geocoder put first (the planner then counts the
    station's platforms and its stops, as for a snapped end), else the point."""
    if matches and chosen is not matches[0] and chosen.get("id"):
        return str(chosen["id"])
    return _point(chosen)


async def _named_place(
    client: Client,
    named: str,
    matches: list[dict[str, Any]],
    chosen: dict[str, Any],
    *,
    language: str,
    flights: bool,
) -> dict[str, Any]:
    """The place chosen, or the stop of the transport place a name names when
    the geocoder's first results held none (staging -138 wording "aeroport de
    Geneve (Meyrin)": the aerodrome first, then four bus stops named after
    its buildings, the station not among them). One stop search, cached,
    with the geocoder's own name for the aerodrome or the name asked without
    its qualifiers, under the rules of ``named_stop``."""
    if not matches or chosen is not matches[0]:
        return chosen
    terms = _named_terms(named, matches, flights)
    if terms is None:
        return chosen
    first = matches[0]
    own = str(first.get("name") or "") if _feature_kind(first, flights) else ""
    text = core_name(own or named)
    try:
        stops = await client.stops_found(text, language, _point(first))
    except TransitError:
        return chosen
    return _stop_for(first, stops, *terms, flights=flights) or chosen


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
    origin_match: dict[str, Any] | None = None
    origins: list[dict[str, Any]] = []
    if request.from_device_position and position is not None:
        start = position
        origin_label = w["position"]
        origin_zone = home_zone
    elif request.origin:
        origins = await client.geocode(request.origin, language, position)
        found = origin_match = await _named_place(
            client,
            request.origin,
            origins,
            choose(
                origins,
                biased=position is not None,
                named=request.origin,
                flights=request.flights,
            ),
            language=language,
            flights=request.flights,
        )
        start, origin_label, origin_zone = (
            _point(found),
            label(found),
            str(found.get("tz") or ""),
        )
    else:
        raise TransitError("origin_missing")
    origin_kind = (
        str(origin_match.get("type") or "") if origin_match is not None else "POSITION"
    )
    destinations = await client.geocode(request.destination, language, start)
    target = await _named_place(
        client,
        request.destination,
        destinations,
        choose(
            destinations,
            biased=False,
            named=request.destination,
            flights=request.flights,
        ),
        language=language,
        flights=request.flights,
    )
    zone = zone_of(target.get("tz"), origin_zone or home_zone)
    deadline = local_time(request.arrive_by, zone, now, days)
    leave = local_time(request.depart_at, zone_of(origin_zone, zone.key), now, days)
    when = deadline or leave or now.astimezone(zone)
    flights = request.flights
    ends: tuple[tuple[float, float] | str, tuple[float, float] | str] = (
        _end(origin_match, origins) if origin_match is not None else start,
        _end(target, destinations),
    )
    response = await client.plan(
        *ends,
        when,
        arrive_by=deadline is not None,
        language=language,
        flights=flights,
    )
    snapped = ""
    if not parse(response, zone, flights=flights):
        # A place the planner's walk search cannot attach to the street
        # network (staging -129: the "King Street, Sydney" street point had no
        # stop within reach, 0 start offsets) gets no itinerary at all: the
        # plan starts or ends at the nearest stop instead, said as such.
        try:
            retried = await _snapped(
                client,
                response,
                start=(
                    start,
                    origin_kind,
                    origin_label,
                    str((origin_match or {}).get("name") or ""),
                ),
                end=(
                    _point(target),
                    str(target.get("type") or ""),
                    label(target),
                    str(target.get("name") or ""),
                ),
                language=language,
                arrive_by=deadline is not None,
                flights=flights,
            )
            if retried is not None:
                response = await client.plan(
                    *retried[0],
                    when,
                    arrive_by=deadline is not None,
                    language=language,
                    flights=flights,
                )
                ends, snapped = retried
        except TransitError:
            # The first answer stands (no itinerary): a failed retry is no
            # reason to hide it.
            snapped = ""
    itineraries, verdict = selected(
        parse(response, zone, flights=flights), now=now, deadline=deadline
    )
    destination_label = label(target)
    distance = _distance_km(start, _point(target))
    doubt = doubts(itineraries, distance, when)
    note, on_time_doubt = "", ""
    if doubt or verdict == "no_itinerary":
        # One more search before giving up: a plan that is implausible or
        # empty is no answer while a reasonable itinerary arrives a little
        # after the deadline, or leaves a little later.
        better = await _reasonable(
            client,
            ends,
            when,
            deadline=deadline,
            now=now,
            zone=zone,
            distance_km=distance,
            language=language,
            flights=flights,
        )
        if better is not None:
            itineraries, late = better
            on_time_doubt = doubt_reason(doubt, language) if doubt else ""
            doubt = {}
            verdict = (
                "after_deadline" if late else ("on_time" if deadline else "listed")
            )
            if late and deadline is not None:
                at = _clock(deadline, deadline.date())
                note = (
                    w["late_doubt"].format(d=at, why=on_time_doubt)
                    if on_time_doubt
                    else w["late_empty"].format(d=at)
                )
    rendered, passages = render(
        itineraries,
        origin=origin_label,
        destination=destination_label,
        deadline=deadline,
        language=language,
        config=config,
        doubt=doubt,
        snapped=snapped if itineraries else "",
        note=note,
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
    outcome = (
        ("doubtful" if doubt else "planned_late" if note else "planned")
        if itineraries
        else verdict
    )
    data: dict[str, Any] = {
        "outcome": outcome,
        "verdict": verdict,
        "day": when.date().isoformat(),
        "weekday": when.strftime("%A"),
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
        data["instruction"] = INSTRUCTION
        if note:
            data["next_step"] = NEXT_STEPS["planned_late"]
            if on_time_doubt:
                data["on_time_doubt"] = on_time_doubt
        if doubt:
            data["doubts"] = doubt
            data["doubt_reason"] = doubt_reason(doubt, language)
            data["next_step"] = NEXT_STEPS["doubtful"]
            data["direct_query"] = await _direct_query(
                client, start, origin_match, target, language
            )
            data["direct_destination"] = str(target.get("name") or "")
    else:
        data["next_step"] = NEXT_STEPS.get(verdict, FALLBACK)
        if verdict == "none_by_deadline":
            data["rendered"] = rendered
    return data


def _served(stop: dict[str, Any], flights: bool) -> bool:
    """A stop the plan may end at: never one served by flights alone (an
    airport's own flight stop) unless the person asked to fly."""
    modes = stop.get("modes")
    if flights or not isinstance(modes, list) or not modes:
        return True
    return any(mode != "AIRPLANE" for mode in modes)


async def _reasonable(
    client: Client,
    ends: tuple[tuple[float, float] | str, tuple[float, float] | str],
    when: datetime,
    *,
    deadline: datetime | None,
    now: datetime,
    zone: ZoneInfo,
    distance_km: float,
    language: str,
    flights: bool,
) -> tuple[list[dict[str, Any]], bool] | None:
    """The reasonable itineraries of a second search, when the plan found is
    implausible or empty, and whether they arrive after the deadline; None
    when there is none (the first plan then stands).

    With a deadline, the search asks for arrivals up to LATE_MINUTES later:
    reasonable itineraries that arrive in time are written as usual, else the
    first ones that arrive late, each saying by how much. Without one, it
    looks over a wider window of departures. One plan request, under the
    cache and the daily cap like any other.
    """
    try:
        response = await client.plan(
            *ends,
            deadline + timedelta(minutes=LATE_MINUTES) if deadline else when,
            arrive_by=deadline is not None,
            language=language,
            flights=flights,
            window=SEARCH_WINDOW if deadline else WIDE_WINDOW,
        )
    except TransitError:
        return None
    found = [
        itinerary
        for itinerary in parse(response, zone, flights=flights)
        if not _slow(itinerary, distance_km) and not _at_night(itinerary, when)
    ]
    kept, _ = selected(found, now=now, deadline=deadline)
    if kept:
        return kept, False
    if deadline is None:
        return None
    limit = deadline + timedelta(minutes=LATE_MINUTES)
    late = [
        i
        for i in found
        if i["depart"] >= now - timedelta(minutes=1) and deadline < i["latest"] <= limit
    ]
    if not late:
        return None
    # The earliest arrivals; of the same arrival, the latest departure.
    late.sort(key=lambda i: (i["latest"], -i["depart"].timestamp(), i["transfers"]))
    return sorted(late[:OPTIONS], key=lambda i: i["depart"]), True


async def _snapped(
    client: Client,
    response: dict[str, Any],
    *,
    start: tuple[tuple[float, float], str, str, str],
    end: tuple[tuple[float, float], str, str, str],
    language: str,
    arrive_by: bool = False,
    flights: bool = False,
) -> tuple[tuple[tuple[float, float] | str, tuple[float, float] | str], str] | None:
    """The plan ends with each unreached place replaced by its nearest stop,
    and the sentence that says so; None when no end can be replaced.

    Each end is (point, geocoder type, label, name). The planner's debug
    counts are those of its search: an arrive-by search runs backwards from
    the destination, so its "start" offsets are the destination's (staging
    -131: "King Street" had stops 110 m away; the Sydney Airport point, amid
    the runways, had none, and the start was snapped instead).

    A destination the planner reaches without any itinerary ends at a stop
    named after it (live 2026-11-20: a Geneva airport point reached two
    stops, the airport's own flight stops; without flights, no itinerary).
    """
    debug = response.get("debugOutput")
    debug = debug if isinstance(debug, dict) else {}
    keys = (
        ("n_dest_offsets", "n_start_offsets")
        if arrive_by
        else (
            "n_start_offsets",
            "n_dest_offsets",
        )
    )
    ends: list[tuple[float, float] | str] = [start[0], end[0]]
    said: list[str] = []
    for index, ((point, kind, place, name), key) in enumerate(
        zip((start, end), keys, strict=True)
    ):
        if kind == "STOP":
            continue
        reached = key in debug and debug[key] != 0
        if reached and index == 0:
            continue
        near = (
            []
            if reached
            else [
                (_distance_km(point, _point(stop)), stop)
                for stop in await client.stops_near(point)
                if _served(stop, flights)
            ]
        )
        near = [pair for pair in near if pair[0] <= SNAP_KM]
        if not near and name.strip():
            # No stop at the point itself: a stop named after the place (its
            # terminal, its station) stands for it within NAMED_KM.
            near = [
                (_distance_km(point, _point(stop)), stop)
                for stop in await client.stops_named(name, language, point)
                if _served(stop, flights)
            ]
            near = [pair for pair in near if pair[0] <= NAMED_KM]
        if not near:
            continue
        distance, stop = min(near, key=lambda pair: pair[0])
        ends[index] = str(stop.get("id") or "") or _point(stop)
        said.append(
            words(language)["snapped" if index == 0 else "snapped_end"].format(
                place=_clean(place),
                stop=_clean(label(stop)),
                km=f"{distance:.1f}",
            )
        )
    if not said:
        return None
    return (ends[0], ends[1]), " ".join(said)


async def _direct_query(
    client: Client,
    start: tuple[float, float],
    origin: dict[str, Any] | None,
    target: dict[str, Any],
    language: str,
) -> str:
    """The web search for the direct services of a doubtful trip: the start's
    town (never the person's street or position) and the destination's name."""
    town = ""
    if origin is not None and origin.get("type") == "PLACE":
        town = str(origin.get("name") or "")
    elif origin is not None and origin.get("type") == "STOP":
        # A stop's name is no town (staging -132: "Gratte-Ciel Rollet" found
        # pages about skyscrapers and a cyclist, nothing about the trains).
        town = _area(origin) or str(origin.get("name") or "")
    elif origin is not None:
        town = _area(origin)
    else:
        try:
            for match in await client.geocode_reverse(start):
                if town := _area(match):
                    break
        except TransitError:
            town = ""
    destination = str(target.get("name") or "").strip()
    if not town.strip() or not destination:
        return ""
    return str(words(language)["direct_query"]).format(
        a=_clean(town), b=_clean(destination)
    )


# Words of a destination's name that name no place (five languages): a page
# must name the destination by another word to be about this trip.
_GENERIC = frozenset(
    "airport aeroport aeropuerto aeroporto flughafen station gare bahnhof "
    "hauptbahnhof hbf estacion stazione centrale central centro terminal "
    "international internationale internacional internazionale intl domestic "
    "city ville stadt ciudad citta "
    "de du des la le les the of di del della von der den el".split()
)


def place_words(name: str) -> set[str]:
    """The words of a place name that name it (folded, three letters or more)."""
    return {w for w in _folded(name).replace("-", " ").split() if len(w) >= 3} - (
        _GENERIC
    )


# Words of an origin that only say "here" (an origin written "my location").
_HERE_WORDS = frozenset(
    "here ici hier aqui aca qui qua current actuelle aktueller actual attuale "
    "location position localisation standort ubicacion posicion posizione".split()
)


def from_own_position(request: TransitPlan, asked: list[str]) -> TransitPlan:
    """The request planned from the device position when the person's last
    message starts the trip where they are ("from here", "desde aquí") and the
    origin the model wrote is not one the person named in it.

    Staging -134: "how do I get there from here by train?" was planned from
    "Moosach / Neuhausen-Nymphenburg (Munich)" and "... from here" in Tokyo
    from "Tokyo Station (Chiyoda)", names the model wrote from the reverse
    geocoding of the position; the answers then flagged the walk from the
    person's real position as not covered. The caller passes this only with a
    device position; an origin the person wrote themselves stays.
    """
    from services.orchestrator.location import own_position_origin

    if request.from_device_position or not asked or not own_position_origin(asked[-1]):
        return request
    named = place_words(request.origin or "") - _HERE_WORDS
    if named and named <= place_words(asked[-1]):
        return request
    return request.model_copy(update={"from_device_position": True, "origin": None})


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
    if position is not None and turn is not None:
        request = from_own_position(request, turn.asked)
    # The person's "tomorrow" is the day after theirs where their device is
    # (staging -132: a browser on UTC located in Sydney, where it was already
    # 07:55 on the 20th, planned "tomorrow" for the 20th; the 08:00 deadline
    # was minutes away and no itinerary could make it).
    local = (zone_at(*position) if position is not None else "") or (
        turn.zone if turn is not None else ""
    )
    try:
        async with asyncio.timeout(max(1.0, min(timeout, config.timeout * 3))):
            data = await compute(
                request,
                config=config,
                language=turn.language if turn is not None else "en",
                position=position,
                home_zone=local or home_zone,
                days=turn.trip_dates(local, _now()) if turn is not None else [],
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
    if turn is not None and data.get("itineraries") and data.get("rendered"):
        # The last plan with itineraries is the one the answer gives.
        if data.get("doubts"):
            # The plan is a fallback: the answer leads with the operators'
            # direct services or says that none was found (doubtful_answer).
            turn.doubtful = str(data["rendered"])
            turn.doubt_reason = str(data.get("doubt_reason") or "")
            turn.plan, turn.plan_times, turn.plan_ends = "", set(), []
            turn.plan_starts, turn.plan_origin = [], ""
        else:
            # The plan is the answer's block, given unaltered (planned_answer).
            turn.plan = str(data["rendered"])
            turn.plan_times = _plan_times(data["itineraries"])
            turn.plan_ends = _plan_ends(data["itineraries"])
            turn.plan_starts = _plan_starts(data["itineraries"])
            turn.plan_origin = str(data.get("origin") or "")
            turn.plan_from_device = bool(request.from_device_position)
            turn.doubtful = turn.doubt_reason = ""
    if data.get("rendered"):
        # Computed facts of this turn (D7): the plan as written, and each
        # itinerary as a passage read from the planner, so the travel checks
        # keep its lines, directions, stops and times and nothing else.
        if turn is not None:
            turn.planned = True
            turn.compute(str(data["rendered"]))
            for passage in data.get("passages", []):
                turn.collect({"url": config.attribution_url, "text": passage})
        if (check := grounding.current.get()) is not None:
            check.collect({"text": str(data["rendered"])})
    return {"data": data}


def with_attribution(
    answer: str, computed: list[str], language: str, blocks: tuple[str, ...] = ()
) -> str:
    """An answer that uses a plan keeps the planner's attribution visible
    (Transitous usage policy): added at the end of the body when missing.

    With ``blocks`` (the turn's computed plan blocks), only when the answer
    shows one: a plan with no itinerary on time is no plan given (staging
    -132: Sydney's web-only answer ended with "Itineraries computed with
    Transitous").
    """
    config = settings()
    if config is None or not answer.strip():
        return answer
    if not any(config.attribution_url in text and OSM_URL in text for text in computed):
        return answer
    body, fold, footer = answer.partition("<details>")
    if blocks and not any(
        block.strip() and _block_span(body.splitlines(), block) is not None
        for block in blocks
    ):
        return answer
    if config.attribution_url in body and OSM_URL in body:
        return answer
    line = attribution(language, config)
    return body.rstrip() + "\n\n" + line + ("\n\n" + fold + footer if fold else "")


# --- Doubtful plans: the operators' direct services first ------------------------


def direct_candidates(
    data: object, config: Settings | None = None, destination: str = ""
) -> list[str]:
    """The pages of a direct-services search to read: its first results that
    are not the planner's own attribution pages and, when the destination is
    known, whose title, extract or address names it (staging -132: the first
    two results were encyclopedia articles about the start's stop name, read
    instead of the airport's access page ranked fourth)."""
    config = config or settings()
    own = {OSM_URL, config.attribution_url if config else SOURCES_URL}
    wanted = place_words(destination)
    rows = data.get("results") if isinstance(data, dict) else None
    links: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        link = row.get("link") if isinstance(row, dict) else None
        if not isinstance(link, str) or link in own or link in links:
            continue
        if wanted:
            about = " ".join(
                str(row.get(key) or "") for key in ("title", "snippet", "link")
            )
            if not wanted & set(_folded(about).split()):
                continue
        links.append(link)
    return links[:DIRECT_PAGES]


def with_direct_pages(
    data: dict[str, Any], pages: list[dict[str, str]]
) -> dict[str, Any]:
    """The doubtful plan with the pages code read about the direct services,
    and the next step that puts them first."""
    from services.orchestrator import travel_claims

    turn = travel_claims.current.get()
    if turn is not None:
        turn.direct_pages = [page["url"] for page in pages]
    return {
        **data,
        "direct_services": [
            {**page, "text": page["text"][:DIRECT_CHARACTERS]} for page in pages
        ],
        "next_step": NEXT_STEPS["doubtful_read" if pages else "doubtful_unread"],
        "instruction": (
            "The trip's answer comes from `direct_services` first; `rendered` "
            "follows unchanged as the fallback. " + INSTRUCTION
        ),
    }


_CLOCK = re.compile(r"(?<![\d:])([01]?\d|2[0-3])[:h]([0-5]\d)(?![\d:])")
_MD_LINK = re.compile(r"\[([^\]\n]*)\]\((https?://[^)\s]+)\)")


def _clocks(text: str) -> set[str]:
    return {f"{int(h):02d}:{m}" for h, m in _CLOCK.findall(text)}


def _site(url: str) -> str:
    host = url.split("//", 1)[-1].split("/", 1)[0].lower()
    return host.removeprefix("www.")


def _plan_times(itineraries: object) -> set[str]:
    """The clock times (HH:MM) of a plan's itineraries and legs, as data:
    never the deadline the person gave."""
    values: list[object] = []
    for itinerary in itineraries if isinstance(itineraries, list) else []:
        if not isinstance(itinerary, dict):
            continue
        values += [itinerary.get("depart"), itinerary.get("arrive")]
        for leg in itinerary.get("legs") or []:
            if isinstance(leg, dict):
                values += [leg.get("depart"), leg.get("arrive")]
    return {
        value[11:16]
        for value in values
        if isinstance(value, str) and len(value) >= 16 and value[10] == "T"
    }


def _plan_starts(itineraries: object) -> list[str]:
    """The place each itinerary's first leg reaches from the plan's origin."""
    starts: list[str] = []
    for itinerary in itineraries if isinstance(itineraries, list) else []:
        legs = itinerary.get("legs") if isinstance(itinerary, dict) else None
        first = legs[0] if isinstance(legs, list) and legs else None
        target = first.get("to") if isinstance(first, dict) else None
        if isinstance(target, str) and target not in _ENDPOINTS | set(starts):
            starts.append(target)
    return starts


def _plan_ends(itineraries: object) -> list[tuple[str, str]]:
    """Each itinerary's departure and arrival (HH:MM)."""
    ends: list[tuple[str, str]] = []
    for itinerary in itineraries if isinstance(itineraries, list) else []:
        if not isinstance(itinerary, dict):
            continue
        depart, arrive = itinerary.get("depart"), itinerary.get("arrive")
        if isinstance(depart, str) and isinstance(arrive, str):
            ends.append((depart[11:16], arrive[11:16]))
    return ends


def _plain(line: str) -> str:
    return " ".join(line.split())


def _block_lines(block: str) -> list[str]:
    return [_plain(line) for line in block.splitlines() if line.strip()]


def _block_span(lines: list[str], block: str) -> tuple[int, int] | None:
    """The first and last indexes of ``block`` in ``lines`` when they hold it
    unaltered (blank lines and spacing aside); None otherwise."""
    wanted = _block_lines(block)
    filled = [(index, _plain(line)) for index, line in enumerate(lines) if line.strip()]
    texts = [text for _, text in filled]
    size = len(wanted)
    for at in range(len(filled) - size + 1) if wanted else ():
        if texts[at : at + size] == wanted:
            return filled[at][0], filled[at + size - 1][0]
    return None


def _tidy(text: str) -> str:
    return re.sub(r"\n[ \t]*(?:\n[ \t]*)+", "\n\n", text).strip()


def own_urls() -> set[str]:
    """The planner's attribution pages: the plan's own, never a page read."""
    config = settings()
    return {config.attribution_url if config else SOURCES_URL, OSM_URL}


def _web(line: str, own: set[str]) -> bool:
    return any(url not in own for _, url in _MD_LINK.findall(line))


_QUOTES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u00a0": " ", "\u202f": " "})


def _loose(line: str) -> str:
    """A line as a copy of a block line is compared: emphasis, quotes, case
    and the spaces around punctuation aside (staging -132: the model's copy of
    the time-zone note, "publiés;" for the block's "publiés ;", was given
    twice)."""
    text = re.sub(r"[*_`]+", "", line.translate(_QUOTES))
    text = re.sub(r"\s*([;:,.!?])", r"\1", " ".join(text.split()))
    return text.strip(" -").casefold()


def _plan_marks(
    lines: list[str], block: str, clocks: set[str], own: set[str]
) -> list[bool]:
    """Which lines restate the computed plan: a line of its block (or a copy
    of one), a line citing only the planner, or a line without a page link
    giving one of its clock times (staging -131: "Option 2: leave 07:39,
    arrive 11:03")."""
    plan_lines = {_loose(line) for line in _block_lines(block)}

    def planned(line: str) -> bool:
        plain = _plain(line)
        if not plain:
            return False
        if _loose(plain) in plan_lines:
            return True
        if _web(line, own):
            return False
        cited = any(url in own for _, url in _MD_LINK.findall(line))
        return cited or bool(_clocks(_MD_LINK.sub(r"\1", line)) & clocks)

    return [planned(line) for line in lines]


def _notes(block: str) -> set[str]:
    """The block's closing notes (time zones, attribution): the lines after
    its last clock time. A copy of one goes, but never places the block."""
    lines = _block_lines(block)
    last = max((i for i, line in enumerate(lines) if _clocks(line)), default=-1)
    return {_loose(line) for line in lines[last + 1 :]}


# Words of a line opening with the block's title that add nothing to it: the
# person's relative days and what states a deadline, five languages (folded,
# first five letters as _stem gives them).
_TITLE_FILLER = frozenset(
    {
        "tomor",
        "today",
        "tonig",
        "demai",
        "aujou",
        "morge",
        "heute",
        "manan",
        "doman",
        "oggi",
        "befor",
        "until",
        "avant",
        "jusqu",
        "antes",
        "hasta",
        "prima",
        "entro",
        "spate",
        "spaet",
        "lates",
        "ankom",
        "plus",
        "tard",
        "tardi",
    }
)
_TITLE_TRIM = " \t-\u2013\u2014:;,.)]*_"
_TITLE_SEPARATOR = re.compile(r"\s[-\u2013\u2014]\s|[:;]\s")


def _title_words(text: str) -> re.Pattern[str] | None:
    """A pattern matching the words of ``text`` in order at the start of a
    line, whatever emphasis, punctuation or arrows lie between them."""
    words = _WORD.findall(text.translate(_QUOTES))
    if not words:
        return None
    gap = r"[\W_]*+"
    return re.compile(
        r"^" + gap + gap.join(map(re.escape, words)) + r"(?![^\W_])",
        re.IGNORECASE,
    )


def _adds_to_title(rest: str, block: str, language: str) -> bool:
    """Whether what follows a copy of the block's title says something the
    block does not: a word of its own (four letters or more), a time or a
    day it does not give. The plan's days by name, in English and the
    answer's language, the person's relative days and the words stating a
    deadline add nothing (staging -136: "... — for tomorrow, Saturday
    2026-11-21, arriving by 11:00:" over a block giving that day and that
    deadline)."""
    days = set(_ISO_DAY.findall(block))
    if set(_ISO_DAY.findall(rest)) - days or _clocks(rest) - _clocks(block):
        return True
    known = {_stem(word) for word in _words(block)} | _TITLE_FILLER
    for day in days:
        for name in _weekday_names(day, language) | _month_names(day, language):
            known |= {_stem(word) for word in _words(name)}
    content = {
        _stem(word)
        for word in _words(_ISO_DAY.sub(" ", rest))
        if len(word) >= 4 and not any(c.isdigit() for c in word)
    }
    return bool(content - known)


def _month_names(day: str, language: str) -> set[str]:
    """The month of an ISO day, in English and in the answer's language."""
    try:
        value = date.fromisoformat(day)
    except ValueError:
        return set()
    names = {value.strftime("%B").casefold()}
    try:
        from babel.dates import format_date

        names.add(format_date(value, "MMMM", locale=language or "en").casefold())
    except Exception:  # noqa: BLE001 - an unknown locale keeps the English name
        pass
    return names


def _title_copies(
    lines: list[str], marks: list[bool], block: str, own: set[str], language: str
) -> None:
    """Mark the model's lines that open with the block's title, or restate
    it: its label then the trip in other words (staging -136: "Public
    transport plan: your position → South Station (Boston) — for tomorrow,
    Saturday 2026-11-21, arriving by 11:00:" right above the block's own
    "**Public transport plan: your position → South Station (Boston)**").
    Such a line is a copy of the block's title: it goes, and the block takes
    its place. What it adds of its own stays, as a plain sentence; a line
    citing a page read stays whole. The title is the block's own first line,
    so this holds in every language of the block."""
    plan = _block_lines(block)
    if not plan:
        return
    title = re.sub(r"[*_`]+", "", plan[0]).strip()
    label = title.split(":", 1)[0]
    whole, opening = _title_words(title), _title_words(label)
    trip = _title_words(title[len(label) + 1 :]) if ":" in title else None
    for index, line in enumerate(lines):
        if marks[index] or not line.strip() or _web(line, own):
            continue
        text = _MD_LINK.sub(r"\1", line).strip()
        found = whole.match(text) if whole else None
        rest = text[found.end() :] if found else ""
        if found is None:
            # The label, then the trip in other words, or words the block
            # gives ("... for tomorrow:"): a title. A sentence about the plan
            # ("Public transport plan is ...") stays.
            found = opening.match(text) if opening else None
            if found is None:
                continue
            rest = text[found.end() :]
            if not (rest.lstrip(" \t*_").startswith(":") or "\u2192" in rest):
                if not _adds_to_title(rest, block, language):
                    marks[index] = True
                continue
            rest = rest.lstrip(_TITLE_TRIM)
            moved = trip.match(rest) if trip else None
            if moved:
                rest = rest[moved.end() :]
            elif "\u2192" in rest:
                # Only what follows the trip, after a dash or a colon, is
                # the line's own.
                after = rest.split("\u2192", 1)[1]
                cut = _TITLE_SEPARATOR.search(after)
                rest = after[cut.end() :] if cut else ""
        rest = rest.strip(_TITLE_TRIM)
        if not rest or not _adds_to_title(rest, block, language):
            marks[index] = True
            continue
        sentence = rest.rstrip(":").rstrip()
        if not re.search(r"[^\W\d_]", sentence):
            marks[index] = True
            continue
        if sentence[-1] not in ".!?":
            sentence += "."
        lines[index] = sentence[0].upper() + sentence[1:]


def _announcements(
    lines: list[str], marks: list[bool], own: set[str], block_at: int | None = None
) -> None:
    """Mark the lines that only announced what went (they end with ":"): the
    restated plan, or nothing at all (staging -132: a Munich answer ended
    with "All options ... arrive well before your 14:00 deadline:" once its
    list, a restatement of the plan, was taken out). A line just before the
    place of the block introduces the block and stays."""
    for index in range(len(lines) - 1, -1, -1):
        text = lines[index].strip().rstrip("*_ ")
        if marks[index] or not text.endswith(":") or _web(lines[index], own):
            continue
        following = next(
            (
                j
                for j in range(index + 1, len(lines))
                if lines[j].strip() and not marks[j]
            ),
            None,
        )
        end = len(lines) if following is None else following
        if block_at is not None and index < block_at and end >= block_at:
            continue
        if following is None or any(marks[index + 1 : end]):
            marks[index] = True


def without_plans(answer: str, turn: Any) -> tuple[str, bool]:
    """The answer without the computed block it gives unaltered, and whether
    the block opened it.

    The travel checks then read the model's own words only: a computed leg is
    never cut out of the block (staging -131: a Caen plan lost a leg when a
    rewrite cut short was replaced by what the checks kept), and code puts the
    block back after them (planned_answer, doubtful_answer).
    """
    blocks = [block for block in (turn.plan, turn.doubtful) if block.strip()]
    body, fold, footer = answer.partition("<details>")
    lines = body.splitlines()
    taken = False
    first = False
    for block in blocks:
        span = _block_span(lines, block)
        if span is None:
            continue
        taken = True
        first = first or not any(line.strip() for line in lines[: span[0]])
        lines = lines[: span[0]] + lines[span[1] + 1 :]
    if not taken:
        return answer, False
    rest = _tidy("\n".join(lines))
    if fold:
        rest = (rest + "\n\n" if rest else "") + fold + footer
    return rest, first


_ITEM = re.compile(r"^[ \t]*(?:[-*+][ \t]|\d{1,2}[.)][ \t]|\*\*)")


def planned_answer(answer: str, turn: Any, *, first: bool = False) -> str:
    """A planned trip's answer with the computed block exactly as code wrote
    it: every option with all its legs.

    The model may open and advise around the block, never restate it: lines
    restating the plan (a block line, a line citing only the planner, a line
    giving one of its times without a page link) go, and the block takes the
    place of the first of them; with none, it opens the answer when it did,
    else follows its first paragraph (staging -131: a Madrid answer rewritten
    as prose lost its AVE leg, and its option 2 had no leg at all). A copy of
    the block's closing notes goes too, and a line that only announced what
    went (staging -132), and a line opening with the block's title or
    restating it (_title_copies, staging -136).
    """
    block = turn.plan.strip()
    if not block or turn.doubtful:
        return answer
    from services.orchestrator import travel_claims

    body, fold, footer = answer.partition("<details>")
    lines = body.splitlines()
    if _block_span(lines, block) is not None:
        return answer
    own = own_urls()
    language = str(getattr(turn, "language", "") or "en")
    marks = _plan_marks(lines, block, turn.plan_times, own)
    _title_copies(lines, marks, block, own, language)
    notes = _notes(block)
    emptied = _own_sentences(
        lines,
        marks,
        block,
        notes,
        own,
        language,
        covered=_start_covered(turn),
    )
    hits = [
        index
        for index, mark in enumerate(marks)
        if mark and index not in emptied and _loose(_plain(lines[index])) not in notes
    ]
    if hits:
        # A list item or an option heading inside the restated plan goes too.
        for index in range(hits[0], hits[-1] + 1):
            if _ITEM.match(lines[index]) and not _web(lines[index], own):
                marks[index] = True
        at = hits[0]
    elif first:
        at = 0
    else:
        filled = next(
            (i for i, line in enumerate(lines) if line.strip() and not marks[i]),
            len(lines),
        )
        at = next(
            (i for i in range(filled, len(lines)) if not lines[i].strip()), len(lines)
        )
    _announcements(lines, marks, own, at)
    head = "\n".join(line for line, mark in zip(lines[:at], marks[:at]) if not mark)
    tail = "\n".join(line for line, mark in zip(lines[at:], marks[at:]) if not mark)
    parts = [
        _tidy(travel_claims.wellformed(head)),
        block,
        _tidy(travel_claims.wellformed(tail)),
    ]
    final = "\n\n".join(part for part in parts if part)
    return final + ("\n\n" + fold + footer if fold else "")


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[^\W_]+")
_PREFIX = re.compile(r"[ \t]*+(?:[-*+][ \t]++|\d{1,2}[.)][ \t]++)?+")
_ISO_DAY = re.compile(r"(?<![\d-])(\d{4}-\d{2}-\d{2})(?![\d-])")


def _words(text: str) -> list[str]:
    return _WORD.findall(_MD_LINK.sub(r"\1", text).translate(_QUOTES).casefold())


def _note_copy(sentence: str, notes: list[set[str]]) -> bool:
    """A reworded copy of one of the block's closing notes: it has nearly all
    the note's words and adds next to none (staging -133: "All times are local
    (Europe/Berlin) from published timetables; ..." under the block's "Times
    are local (Europe/Berlin), from published timetables; ..."). Words, not
    wording, are compared, so this holds in every language of the notes; a
    sentence adding a clause of its own is no copy.

    A shorter sentence whose content words all come from one note is a copy
    too (staging -134: "Check the times with the operator before you
    travel." under "Times are local (America/New_York), from published
    timetables; check them with the operator before you travel."). Content
    words are those of four letters or more, in any language: one added word
    in seven is a rewording, a place or a fact of its own is not. They are
    compared by their first five letters without accents, so another form
    of a note's verb ("Compruebe los horarios" for "compruébelas") is the
    note's word."""
    words = set(_words(sentence))
    if not words:
        return False
    content = {_stem(word) for word in words if len(word) >= 4 and not word.isdigit()}
    for note in notes:
        if len(note) < 4:
            continue
        added = len(words - note)
        if len(words & note) >= 0.75 * len(note) and added <= max(
            2, int(0.3 * len(words))
        ):
            return True
        stems = {_stem(word) for word in note}
        if len(content & stems) >= 3 and len(content - stems) <= len(content) // 7:
            return True
    return False


def _stem(word: str) -> str:
    return _folded(word)[:5]


def _weekday_names(day: str, language: str) -> set[str]:
    """The weekday of an ISO day, in English and in the answer's language."""
    try:
        value = date.fromisoformat(day)
    except ValueError:
        return set()
    names = {value.strftime("%A").casefold()}
    try:
        from babel.dates import format_date

        names.add(format_date(value, "EEEE", locale=language or "en").casefold())
    except Exception:  # noqa: BLE001 - an unknown locale keeps the English name
        pass
    return names


def _names_day(sentence: str, days: set[str], names: set[str]) -> bool:
    if days & set(_ISO_DAY.findall(sentence)):
        return True
    words = set(_words(sentence))
    return any(set(_words(name)) <= words for name in names if name)


def _sentences(line: str) -> list[str]:
    pieces: list[str] = []
    for piece in _SENTENCE_END.split(line.strip()):
        if pieces and not re.search(r"[^\W\d_]", _MD_LINK.sub("", piece)):
            # A citation after its sentence's full stop belongs to it.
            pieces[-1] += " " + piece
        else:
            pieces.append(piece)
    return pieces


def _own_sentences(
    lines: list[str],
    marks: list[bool],
    block: str,
    notes: set[str],
    own: set[str],
    language: str,
    covered: Callable[[str], bool] | None = None,
) -> set[int]:
    """Take out of the model's own lines the sentences that only repeat the
    block or contradict it, and mark the lines left empty; their indexes are
    returned (they never place the block).

    - A reworded copy of a closing note (_note_copy).
    - A sentence setting one of the plan's days against another day, and the
      sentences after it in its paragraph that name that other day (staging
      -133: told the wrong "today", the model wrote "The journey planner
      returned times for Friday 2026-11-20, not your travel day of Saturday
      2026-11-21. Confirm Saturday's exact times ..." under a plan rightly
      computed for the person's tomorrow). Code computed the plan's days in
      the person's zone; a sentence citing a page read stays.
    - A caveat that the way from the start to the plan's first stop is not
      covered, when the plan's first leg covers it (``covered``, staging
      -134: "The walk from your starting point to Westfriedhof is not covered
      by the sources" under a plan whose first leg is that walk).
    """
    note_words = [set(_words(note)) for note in notes]
    planned = set(_ISO_DAY.findall(block))
    emptied: set[int] = set()
    for index, line in enumerate(lines):
        if marks[index] or not line.strip() or _HEADING.match(line):
            continue
        prefix = _PREFIX.match(line)
        start = prefix.end() if prefix else 0
        pieces = _sentences(line[start:])
        kept: list[str] = []
        disputed: set[str] = set()
        names: set[str] = set()
        for piece in pieces:
            if _web(piece, own):
                kept.append(piece)
                continue
            if _note_copy(piece, note_words):
                continue
            if covered is not None and covered(piece):
                continue
            days = set(_ISO_DAY.findall(piece))
            if planned and days & planned and days - planned:
                disputed |= days - planned
                for day in days - planned:
                    names |= _weekday_names(day, language)
                continue
            if disputed and _names_day(piece, disputed, names):
                continue
            kept.append(piece)
        if len(kept) == len(pieces):
            continue
        text = " ".join(kept).strip()
        if not text or not re.search(r"[^\W\d_]", text):
            marks[index] = True
            emptied.add(index)
        else:
            lines[index] = line[:start] + text
    return emptied


# The trip's starting point as an answer names it, five languages (folded).
_START = re.compile(
    r"(?<![\w-])(?:starting (?:point|location|address)|start(?:ing)? point|"
    r"your (?:position|location|address|start)|where you are|"
    r"point de depart|(?:votre|ta) (?:position|adresse)|(?:la )?ou (?:vous etes|tu es)|"
    r"ausgangs(?:punkt|ort|adresse)|(?:ihrem|deinem) standort|wo (?:sie sind|du bist)|"
    r"punto de partida|(?:tu|su) (?:posicion|ubicacion|direccion)|donde (?:estas|esta usted)|"
    r"punto di partenza|(?:tua|sua) posizione|dove (?:sei|si trova))(?![\w-])"
)


def _start_covered(turn: Any) -> Callable[[str], bool] | None:
    """Whether a sentence only says that the way from the start to a first
    stop of the plan is not covered, when that first leg is in the plan: it
    names one of the stops the first legs reach, and the start as the plan
    names it (or, for a plan from the device position, as "your starting
    point" or the person's own place). A sentence with a clock time says more."""
    starts = [place_words(stop) for stop in getattr(turn, "plan_starts", []) or []]
    starts = [stop for stop in starts if stop]
    if not starts:
        return None
    from services.orchestrator import route_facts

    device = bool(getattr(turn, "plan_from_device", False))
    # The origin as the answer writes it: its name without the area the
    # plan adds in parentheses, and none of the first stops' words.
    origin = (
        set()
        if device
        else place_words(
            re.sub(r"\s*\([^)]*\)", "", str(getattr(turn, "plan_origin", "")))
        ).difference(*starts)
    )
    person = place_words(
        " ".join(
            [str(getattr(turn, "place", "") or ""), *getattr(turn, "place_names", [])]
        )
    ).difference(*starts)

    def covered(sentence: str) -> bool:
        folded = _folded(sentence).replace("'", " ")
        if _clocks(sentence) or not route_facts.gap(folded):
            return False
        words = set(re.findall(r"\w+", folded))
        if not any(stop <= words for stop in starts):
            return False
        if origin and origin <= words:
            return True
        return device and bool(_START.search(folded) or person & words)

    return covered


_HEADING = re.compile(
    r"^[ \t]*+(?:#{1,6}[ \t]|(?:\*\*|__)[^*_\n]++(?:\*\*|__)[ \t]*+:?+[ \t]*+$)"
)


def _read_only(text: str, own: set[str]) -> str:
    """The sentences of ``text`` that cite a page read (a list item whole),
    and the headings of what is kept: for a doubtful plan nothing else of the
    model's words stands (staging -132: "Il n'existe pas de liaison directe ;
    le trajet passe par Lyon puis Genève", no page read, stayed above the
    night chain although direct trains run)."""
    kept: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            kept.append("")
            continue
        if _HEADING.match(line) and not _web(line, own):
            kept.append(line)
            continue
        if _ITEM.match(line):
            kept.append(line if _web(line, own) else "")
            continue
        kept.append(" ".join(piece for piece in _sentences(line) if _web(piece, own)))
    lines = kept
    # A heading stays only above kept text of its own.
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() and _HEADING.match(lines[index]):
            following = next((line for line in lines[index + 1 :] if line.strip()), "")
            if not following or _HEADING.match(following):
                lines[index] = ""
    return _tidy("\n".join(lines))


def doubtful_answer(answer: str, language: str, turn: Any = None) -> str:
    """A doubtful plan's answer: what the pages read support first, the
    computed plan only as its labelled fallback; or, when no page supports a
    direct service, the plain statement that the open data has no reasonable
    connection for this trip (staging -129: the night chain via Annecy was
    given as "the most direct option" to Geneva airport).

    ``turn`` is passed by the pipeline: its post-checks run after the turn's
    context is reset (staging -131: this read an empty context, and the
    Geneva night chain stayed the answer). An answer left empty (it was the
    fallback block alone) gets the statement and the fallback. Only the
    model's sentences citing a page read stand: the open data lacks a service
    here, so nothing else says what runs or does not (staging -132).
    """
    from services.orchestrator import travel_claims

    turn = turn if turn is not None else travel_claims.current.get()
    config = settings()
    if turn is None or not turn.doubtful or config is None:
        return answer
    fallback = turn.doubtful.strip()
    own = {config.attribution_url, OSM_URL}
    body, fold, footer = answer.partition("<details>")
    lines = body.splitlines()
    marks = _plan_marks(lines, fallback, _clocks(fallback), own)
    _announcements(lines, marks, own)
    kept = "\n".join(line for line, mark in zip(lines, marks) if not mark)
    kept = _read_only(_tidy(travel_claims.wellformed(kept)), own)
    w = words(language)
    if kept and _web(kept, own):
        parts = [kept, fallback]
    else:
        links = ", ".join(f"[{_site(url)}]({url})" for url in turn.direct_pages)
        parts = [
            w["no_connection"].format(why=turn.doubt_reason or "-"),
            w["pages"].format(links=links) if links else "",
            fallback,
        ]
    final = "\n\n".join(part for part in parts if part)
    return final + ("\n\n" + fold + footer if fold else "")


_CLAUSE_MARK = re.compile(r"^\s*[,;:(\u2013\u2014]")


def without_fragments(answer: str, before: str, turn: Any) -> str:
    """A planned or doubtful answer without the pieces of the model's own
    sentences that the checks cut short: a line that is the start of one of
    its lines up to a clause mark, the rest gone (staging -132: "From Tokyo
    Station (Chiyoda)" was left of "From Tokyo Station (Chiyoda), three
    direct Nozomi ... get you to Kyoto Station before 10:00:" above the
    block, whose title names the trip)."""
    blocks = [block for block in (turn.plan, turn.doubtful) if block.strip()]
    if not blocks:
        return answer
    originals = [
        _plain(line).casefold()
        for line in before.partition("<details>")[0].splitlines()
        if line.strip()
    ]
    body, fold, footer = answer.partition("<details>")
    lines = body.splitlines()
    inside: set[int] = set()
    for block in blocks:
        span = _block_span(lines, block)
        if span is not None:
            inside.update(range(span[0], span[1] + 1))

    def cut(line: str) -> bool:
        core = re.sub(r"[*_`]+", "", _plain(line)).rstrip(" :,;").casefold()
        if len(core) < 3 or core[-1] in ".!?\u2026" or _ITEM.match(line):
            return False
        return any(
            original.startswith(core)
            and _CLAUSE_MARK.match(original[len(core) :])
            and re.search(r"[^\W\d_]", original[len(core) :])
            for original in (re.sub(r"[*_`]+", "", o) for o in originals)
        )

    kept = [
        line
        for index, line in enumerate(lines)
        if index in inside or not line.strip() or not cut(line)
    ]
    if len(kept) == len(lines):
        return answer
    rest = _tidy("\n".join(kept))
    return rest + ("\n\n" + fold + footer if fold else "")
