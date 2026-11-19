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

    async def plan(
        self,
        origin: tuple[float, float] | str,
        destination: tuple[float, float] | str,
        when: datetime,
        *,
        arrive_by: bool,
        language: str,
    ) -> dict[str, Any]:
        params = {
            "fromPlace": _place(origin),
            "toPlace": _place(destination),
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


def _place(value: tuple[float, float] | str) -> str:
    """A plan end: coordinates, or a stop id as the geocoder returned it."""
    if isinstance(value, str):
        return value
    return f"{value[0]:.5f},{value[1]:.5f}"


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
    hours = max(fastest["minutes"], 1) / 60
    if distance_km >= DOUBT_KM and distance_km / hours < DOUBT_KMH:
        found["slow"] = {
            "km": round(distance_km),
            "minutes": fastest["minutes"],
            "kmh": round(distance_km / hours),
        }
    if asked is not None and asked.hour >= NIGHT_END + 1:
        night = [
            i
            for i in itineraries
            if i["depart"].hour < NIGHT_END or i["depart"].date() < asked.date()
        ]
        if len(night) == len(itineraries):
            found["night"] = {"depart": fastest["depart"].strftime("%H:%M")}
    return found


# --- Wording -----------------------------------------------------------------
# Five interface languages; non-ASCII letters are written as escapes.

WORDS: dict[str, dict[str, Any]] = {
    "en": {
        "fallback_title": "Fallback only, computed from the open timetable data",
        "snapped": "The planner reaches no stop from {place} itself: the plan starts at {stop}, the nearest stop ({km} km in a straight line).",
        "no_connection": "No reasonable public transport connection was found in the open timetable data for this trip: {why}. No page read for this answer confirms a direct service either: check the operators' timetables.",
        "pages": "Pages read about the direct services: {links}.",
        "direct_query": "{a} {b} direct train or coach timetable",
        "doubt": "Caution: {why}. The open timetable data may lack a direct service on this route (for example a cross-border train): check the operators' timetables before you rely on this plan.",
        "doubt_slow": "the fastest itinerary found takes {m} for {km} km",
        "doubt_night": "every itinerary found leaves at night (from {t})",
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
        "no_connection": "Aucune liaison raisonnable en transports en commun n’a été trouvée dans les données horaires ouvertes pour ce trajet : {why}. Aucune page consultée pour cette réponse ne confirme non plus de liaison directe : vérifiez les horaires des exploitants.",
        "pages": "Pages consultées sur les liaisons directes : {links}.",
        "direct_query": "{a} {b} train ou car direct horaires",
        "doubt": "Attention : {why}. Les donn\u00e9es horaires ouvertes n\u2019ont peut-\u00eatre pas de liaison directe sur ce trajet (par exemple un train transfrontalier) : v\u00e9rifiez les horaires des exploitants avant de vous fier \u00e0 ce plan.",
        "doubt_slow": "l\u2019itin\u00e9raire le plus rapide trouv\u00e9 met {m} pour {km} km",
        "doubt_night": "tous les itin\u00e9raires trouv\u00e9s partent la nuit (d\u00e8s {t})",
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
        "no_connection": "In den offenen Fahrplandaten wurde für diese Reise keine sinnvolle Verbindung mit öffentlichen Verkehrsmitteln gefunden: {why}. Auch keine für diese Antwort gelesene Seite bestätigt eine direkte Verbindung: Prüfen Sie die Fahrpläne der Verkehrsunternehmen.",
        "pages": "Gelesene Seiten zu direkten Verbindungen: {links}.",
        "direct_query": "{a} {b} direkter Zug oder Fernbus Fahrplan",
        "doubt": "Achtung: {why}. In den offenen Fahrplandaten fehlt auf dieser Strecke vielleicht eine direkte Verbindung (zum Beispiel ein grenz\u00fcberschreitender Zug): pr\u00fcfen Sie die Fahrpl\u00e4ne der Verkehrsunternehmen, bevor Sie sich auf diesen Plan verlassen.",
        "doubt_slow": "die schnellste gefundene Verbindung braucht {m} f\u00fcr {km} km",
        "doubt_night": "alle gefundenen Verbindungen fahren nachts ab (ab {t})",
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
        "no_connection": "No se encontró ninguna conexión razonable en transporte público en los datos horarios abiertos para este trayecto: {why}. Ninguna página consultada para esta respuesta confirma tampoco un servicio directo: compruebe los horarios de los operadores.",
        "pages": "Páginas consultadas sobre los servicios directos: {links}.",
        "direct_query": "{a} {b} tren o autobús directo horarios",
        "doubt": "Atenci\u00f3n: {why}. Puede que los datos horarios abiertos no tengan un servicio directo en este trayecto (por ejemplo un tren transfronterizo): compruebe los horarios de los operadores antes de fiarse de este plan.",
        "doubt_slow": "el itinerario m\u00e1s r\u00e1pido encontrado tarda {m} para {km} km",
        "doubt_night": "todos los itinerarios encontrados salen de noche (desde las {t})",
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
        "no_connection": "Nei dati orari aperti non è stato trovato alcun collegamento ragionevole con i mezzi pubblici per questo tragitto: {why}. Nessuna pagina consultata per questa risposta conferma neppure un servizio diretto: verifichi gli orari degli operatori.",
        "pages": "Pagine consultate sui servizi diretti: {links}.",
        "direct_query": "{a} {b} treno o pullman diretto orari",
        "doubt": "Attenzione: {why}. I dati orari aperti forse non hanno un servizio diretto su questo tragitto (per esempio un treno transfrontaliero): verifichi gli orari degli operatori prima di affidarsi a questo piano.",
        "doubt_slow": "l\u2019itinerario pi\u00f9 veloce trovato impiega {m} per {km} km",
        "doubt_night": "tutti gli itinerari trovati partono di notte (dalle {t})",
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
) -> tuple[str, list[str]]:
    """The plan as Markdown, and one passage per itinerary (its legs as
    sentences, each with its line, direction, stops and times)."""
    w = words(language)
    # A doubtful plan is written as a fallback, never as the trip's answer
    # (staging -129: a night chain via Annecy was given as the way to Geneva).
    title = w["fallback_title"] if doubt else w["title"]
    lines = [f"**{title}: {_clean(origin)} \u2192 {_clean(destination)}**", ""]
    if doubt:
        caveat = w["doubt"].format(why=doubt_reason(doubt, language))
        lines[1:1] = ["", f"_{caveat}_"]
    if snapped:
        lines.insert(1 + 2 * bool(doubt), "")
        lines.insert(2 + 2 * bool(doubt), snapped)
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
    if request.from_device_position and position is not None:
        start = position
        origin_label = w["position"]
        origin_zone = home_zone
    elif request.origin:
        found = origin_match = choose(
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
    origin_kind = (
        str(origin_match.get("type") or "") if origin_match is not None else "POSITION"
    )
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
    snapped = ""
    if not parse(response, zone):
        # A place the planner's walk search cannot attach to the street
        # network (staging -129: the "King Street, Sydney" street point had no
        # stop within reach, 0 start offsets) gets no itinerary at all: the
        # plan starts or ends at the nearest stop instead, said as such.
        try:
            retried = await _snapped(
                client,
                response,
                start=(start, origin_kind, origin_label),
                end=(_point(target), str(target.get("type") or ""), label(target)),
                language=language,
            )
            if retried is not None:
                ends, snapped = retried
                response = await client.plan(
                    ends[0],
                    ends[1],
                    when,
                    arrive_by=deadline is not None,
                    language=language,
                )
        except TransitError:
            # The first answer stands (no itinerary): a failed retry is no
            # reason to hide it.
            snapped = ""
    itineraries, verdict = selected(parse(response, zone), now=now, deadline=deadline)
    destination_label = label(target)
    doubt = doubts(itineraries, _distance_km(start, _point(target)), when)
    rendered, passages = render(
        itineraries,
        origin=origin_label,
        destination=destination_label,
        deadline=deadline,
        language=language,
        config=config,
        doubt=doubt,
        snapped=snapped if itineraries else "",
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
    outcome = ("doubtful" if doubt else "planned") if itineraries else verdict
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
        if doubt:
            data["doubts"] = doubt
            data["doubt_reason"] = doubt_reason(doubt, language)
            data["next_step"] = NEXT_STEPS["doubtful"]
            data["direct_query"] = await _direct_query(
                client, start, origin_match, target, language
            )
    else:
        data["next_step"] = NEXT_STEPS.get(verdict, FALLBACK)
        if verdict == "none_by_deadline":
            data["rendered"] = rendered
    return data


async def _snapped(
    client: Client,
    response: dict[str, Any],
    *,
    start: tuple[tuple[float, float], str, str],
    end: tuple[tuple[float, float], str, str],
    language: str,
) -> tuple[tuple[tuple[float, float] | str, tuple[float, float] | str], str] | None:
    """The plan ends with each unreached place replaced by its nearest stop,
    and the sentence that says so; None when no end can be replaced."""
    debug = response.get("debugOutput")
    debug = debug if isinstance(debug, dict) else {}
    ends: list[tuple[float, float] | str] = [start[0], end[0]]
    said: list[str] = []
    for index, ((point, kind, name), key) in enumerate(
        ((start, "n_start_offsets"), (end, "n_dest_offsets"))
    ):
        if kind == "STOP":
            continue
        if key in debug and debug[key] != 0:
            continue
        near = [
            (_distance_km(point, _point(stop)), stop)
            for stop in await client.stops_near(point)
        ]
        near = [pair for pair in near if pair[0] <= SNAP_KM]
        if not near:
            continue
        distance, stop = min(near, key=lambda pair: pair[0])
        ends[index] = str(stop.get("id") or "") or _point(stop)
        said.append(
            words(language)["snapped"].format(
                place=_clean(name),
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
    if origin is not None and origin.get("type") in {"STOP", "PLACE"}:
        town = str(origin.get("name") or "")
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
    if turn is not None and data.get("doubts") and data.get("rendered"):
        # The plan is a fallback: the answer leads with the operators' direct
        # services or says that none was found (doubtful_answer).
        turn.doubtful = str(data["rendered"])
        turn.doubt_reason = str(data.get("doubt_reason") or "")
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


# --- Doubtful plans: the operators' direct services first ------------------------


def direct_candidates(data: object, config: Settings | None = None) -> list[str]:
    """The pages of a direct-services search to read: its first results that
    are not the planner's own attribution pages."""
    config = config or settings()
    own = {OSM_URL, config.attribution_url if config else SOURCES_URL}
    rows = data.get("results") if isinstance(data, dict) else None
    links: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        link = row.get("link") if isinstance(row, dict) else None
        if isinstance(link, str) and link not in own and link not in links:
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


def doubtful_answer(answer: str, language: str) -> str:
    """A doubtful plan's answer: what the pages read support first, the
    computed plan only as its labelled fallback; or, when no page supports a
    direct service, the plain statement that the open data has no reasonable
    connection for this trip (staging -129: the night chain via Annecy was
    given as "the most direct option" to Geneva airport)."""
    from services.orchestrator import travel_claims

    turn = travel_claims.current.get()
    config = settings()
    if turn is None or not turn.doubtful or config is None or not answer.strip():
        return answer
    fallback = turn.doubtful.strip()
    own = {config.attribution_url, OSM_URL}
    plan_lines = {
        " ".join(line.split()) for line in fallback.splitlines() if line.strip()
    }
    plan_clocks = _clocks(fallback)
    body, fold, footer = answer.partition("<details>")
    lines = body.splitlines()

    def web(line: str) -> bool:
        return any(url not in own for _, url in _MD_LINK.findall(line))

    def planned(line: str) -> bool:
        plain = " ".join(line.split())
        if not plain:
            return False
        if plain in plan_lines:
            return True
        if web(line):
            return False
        cited = any(url in own for _, url in _MD_LINK.findall(line))
        return cited or bool(_clocks(_MD_LINK.sub(r"\1", line)) & plan_clocks)

    marks = [planned(line) for line in lines]
    for index in range(len(lines) - 1, -1, -1):
        # A line that only announces the plan ("the most direct option found:")
        # goes with it.
        text = lines[index].strip().rstrip("*_ ")
        if marks[index] or not text.endswith(":") or web(lines[index]):
            continue
        following = next(
            (j for j in range(index + 1, len(lines)) if lines[j].strip()), None
        )
        if following is not None and marks[following]:
            marks[index] = True
    kept = "\n".join(line for line, mark in zip(lines, marks) if not mark)
    kept = travel_claims.wellformed(kept).strip()
    w = words(language)
    if kept and web(kept):
        parts = [kept, fallback]
    else:
        links = ", ".join(f"[{_site(url)}]({url})" for url in turn.direct_pages)
        parts = [
            w["no_connection"].format(why=turn.doubt_reason or "-"),
            w["pages"].format(links=links) if links else "",
            kept,
            fallback,
        ]
    final = "\n\n".join(part for part in parts if part)
    return final + ("\n\n" + fold + footer if fold else "")
