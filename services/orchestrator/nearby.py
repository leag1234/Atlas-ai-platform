"""OSM candidates and opening-hours evaluation; request coordinates never persist."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo
from typing import Any

import aiohttp
from opening_hours import OpeningHours, State, validate

from services.orchestrator.cache import Cache
from services.orchestrator.location import (
    CATEGORIES,
    Position,
    categories,
    category_tags,
)


# A word joining a street or landmark to its town in free text (fr, en, de, es, it).
PLACE_JOINER = re.compile(
    r"\s+(?:à|a|au|aux|in|at|near|bei|en|a la|in der)\s+", re.IGNORECASE
)


class NearbyError(ValueError):
    """Only fixed error codes cross the provider boundary."""


def distance(origin: Position, latitude: float, longitude: float) -> int:
    a, b = math.radians(origin.latitude), math.radians(latitude)
    d = (
        math.sin((b - a) / 2) ** 2
        + math.cos(a)
        * math.cos(b)
        * math.sin(math.radians(longitude - origin.longitude) / 2) ** 2
    )
    return round(6371000 * 2 * math.asin(min(1, math.sqrt(d))) / 50) * 50


def opening_status(
    expression: str | None, coords: tuple[float, float], now: datetime
) -> dict[str, Any]:
    now = now.astimezone(ZoneInfo("UTC"))
    unknown = dict(
        status="unknown",
        closing_time=None,
        next_opening=None,
        closing_soon=False,
        timezone=None,
    )
    if (
        not isinstance(expression, str)
        or not expression
        or len(expression) > 1000
        or not validate(expression)
    ):
        return unknown
    try:
        hours = OpeningHours(expression, coords=coords, max_interval_days=8)
        if hours.warnings or re_school_holiday(expression):
            return unknown
        state, _ = hours.state(now)
        # Find an actual opening/closing, rather than a comment-only change.
        closing, opening = None, None
        for start, end, interval_state, _ in hours.intervals(
            now, now + timedelta(days=8)
        ):
            if state == State.OPEN and interval_state != State.OPEN:
                closing = start
                break
            if state != State.OPEN and interval_state == State.OPEN:
                opening = start
                break
        local = next(hours.intervals(now, now + timedelta(minutes=1)))[0]
        return dict(
            status={
                State.OPEN: "open",
                State.CLOSED: "closed",
                State.UNKNOWN: "unknown",
            }[state],
            closing_time=closing.isoformat() if closing else None,
            next_opening=opening.isoformat() if opening else None,
            closing_soon=bool(closing and closing - now <= timedelta(minutes=30)),
            timezone=str(local.tzinfo),
        )
    except (ValueError, SyntaxError, OverflowError):
        return unknown


def re_school_holiday(expression: str) -> bool:
    # The parser has public-holiday calendars, but no school-holiday calendars.
    import re

    return bool(re.search(r"\bSH\b", expression))


# How many closer places without listed hours are added to the five shown
# (nearby_presentation shows up to eight).
UNKNOWN_EXTRA = 3


def rank(
    candidates: list[dict[str, Any]], open_now: bool = True
) -> list[dict[str, Any]]:
    def key(place: dict[str, Any]) -> tuple[Any, ...]:
        status = place["opening"]["status"]
        completeness = sum(
            bool(place.get(k)) for k in ("address", "website", "phone", "opening_hours")
        )
        return (
            (0 if status == "open" else 1 if place["opening"]["next_opening"] else 2)
            if open_now
            else 0,
            place["opening"]["next_opening"]
            if open_now and status != "open" and place["opening"]["next_opening"]
            else "",
            place["distance_m"],
            -completeness,
            place["name"],
            place["source"],
        )

    ordered = sorted(candidates, key=key)
    # Preserve one choice per requested category when a mixed request has many
    # closer bakeries and only one convenience shop.
    chosen = ordered[:5]
    for category in dict.fromkeys(p["category"] for p in ordered):
        if not any(p["category"] == category for p in chosen):
            candidate = next(p for p in ordered if p["category"] == category)
            replace = next(
                (
                    i
                    for i in range(len(chosen) - 1, -1, -1)
                    if sum(p["category"] == chosen[i]["category"] for p in chosen) > 1
                ),
                None,
            )
            if replace is not None:
                chosen[replace] = candidate
    # Opening first hides a place without listed hours that is closer than the
    # places shown (owner report 2026-11-13: close bakeries were missing). It may
    # well be open: keep the nearest few, in their own "hours not listed" group.
    if open_now and chosen:
        farthest = max(p["distance_m"] for p in chosen)
        closer_unknown = [
            p
            for p in sorted(ordered, key=lambda p: p["distance_m"])
            if p["opening"]["status"] == "unknown"
            and not p["opening"]["next_opening"]
            and p["distance_m"] < farthest
            and not any(p is c for c in chosen)
        ]
        chosen.extend(closer_unknown[:UNKNOWN_EXTRA])
    return sorted(chosen, key=key)


class OSM:
    def __init__(self) -> None:
        self.root = Path(os.environ.get("ATLAS_OSM_STATE", "BRAIN/osm"))
        self.cache = Cache(self.root / "public-places.sqlite")
        self.queries = 0
        self.served_by = ""
        self.failures: list[str] = []
        self.area = ""

    async def request(
        self, service: str, parameters: dict[str, str], endpoint: str | None = None
    ) -> Any:
        from packages.privacy import require_shared

        # A public map service receives the place asked: not in strict privacy.
        require_shared("places_search")
        contact = os.environ.get(
            "ATLAS_OSM_CONTACT", "https://github.com/leag1234/atlas-ai-platform/issues"
        ).strip()
        if not contact or any(c in contact for c in "\r\n"):
            raise NearbyError("osm_contact_not_configured")
        endpoint = endpoint or os.environ.get(
            "ATLAS_NOMINATIM_URL" if service == "geocode" else "ATLAS_OVERPASS_URL",
            "https://nominatim.openstreetmap.org/search"
            if service == "geocode"
            else "https://overpass-api.de/api/interpreter",
        )
        if not endpoint or not endpoint.startswith(("https://", "http://")):
            raise NearbyError("osm_endpoint_invalid")
        # Cross-process rate limit; lock files contain a timestamp only. Waiting
        # for LOCK_NB remains cancellable and cannot strand a worker thread.
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / (service + ".rate")).open("a+") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.1)
            lock.seek(0)
            previous = float(lock.read() or 0)
            await asyncio.sleep(
                max(0, previous + (1.05 if service == "geocode" else 5) - time.time())
            )
            lock.seek(0)
            lock.truncate()
            lock.write(str(time.time()))
            lock.flush()
            self.queries += 1
            try:
                async with aiohttp.ClientSession(
                    trust_env=False,
                    timeout=aiohttp.ClientTimeout(
                        total=12 if service == "geocode" else 4
                    ),
                    headers={
                        "User-Agent": "ATLAS-Nearby/1.0 (+https://github.com/leag1234/atlas-ai-platform; "
                        + contact
                        + ")"
                    },
                ) as client:
                    async with client.request(
                        "GET" if service == "geocode" else "POST",
                        endpoint,
                        params=parameters if service == "geocode" else None,
                        data=parameters if service != "geocode" else None,
                        allow_redirects=False,
                    ) as response:
                        if response.status != 200:
                            raise NearbyError("osm_http_" + str(response.status))
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            body.extend(chunk)
                            if len(body) > 4_000_000:
                                raise NearbyError("osm_response_too_large")
                        return json.loads(body)
            except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError):
                raise NearbyError("osm_unavailable") from None

    async def geocode(self, place: str) -> tuple[Position, str]:
        # This is a user-triggered lookup, never autocomplete. No lookup query,
        # response or resolved user position is retained by the server cache.
        data = await self.request(
            "geocode",
            {"q": place, "format": "jsonv2", "limit": "1", "addressdetails": "1"},
        )
        # People write "Station Square in Caen" or "Main Street in Leeds"; the
        # search engine wants "Station Square, Caen". One retry, same policy.
        joined = PLACE_JOINER.sub(", ", place)
        if not data and joined != place:
            data = await self.request(
                "geocode",
                {"q": joined, "format": "jsonv2", "limit": "1", "addressdetails": "1"},
            )
        try:
            row = data[0]
            if not isinstance(row, dict):
                raise NearbyError("place_not_found")
            address = row.get("address", {})
            if not isinstance(address, dict):
                raise NearbyError("place_not_found")
            area = next(
                (
                    str(address[k])
                    for k in (
                        "city",
                        "town",
                        "village",
                        "municipality",
                        "county",
                        "country",
                    )
                    if address.get(k)
                ),
                "approximate area",
            )
            self.area = area[:100]
            return Position(
                latitude=float(row["lat"]), longitude=float(row["lon"])
            ), area[:100]
        except (KeyError, IndexError, TypeError, ValueError):
            raise NearbyError("place_not_found") from None

    async def candidates(
        self,
        origin: Position,
        category: str,
        radius: int,
        now: datetime,
        language: str = "en",
    ) -> list[dict[str, Any]]:
        requested = categories(category)
        if not requested:
            raise NearbyError("category_not_supported")
        # Cache a public geographic cell, shared across users. It contains POIs
        # only, never the search origin, its distance, user id, or request text.
        # Ten-kilometre padding gives complete coverage at the supported radius.
        lat_cell, lon_cell = (
            math.floor(origin.latitude * 10),
            math.floor(origin.longitude * 10),
        )
        cell_lat, cell_lon = (lat_cell + 0.5) / 10, (lon_cell + 0.5) / 10
        lon_pad = 0.1 / max(0.01, math.cos(math.radians(cell_lat)))
        bbox = f"{max(-90, cell_lat - 0.15)},{max(-180, cell_lon - 0.05 - lon_pad)},{min(90, cell_lat + 0.15)},{min(180, cell_lon + 0.05 + lon_pad)}"
        endpoints = [
            url.strip()
            for url in os.environ.get(
                "ATLAS_OVERPASS_URLS",
                os.environ.get(
                    "ATLAS_OVERPASS_URL", "https://overpass-api.de/api/interpreter"
                ),
            ).split(",")
            if url.strip()
        ][:3]
        nominatim = os.environ.get(
            "ATLAS_NOMINATIM_URL", "https://nominatim.openstreetmap.org/search"
        )
        key = (
            "public-cell:"
            + hashlib.sha256(
                f"{endpoints}:{nominatim}:{lat_cell}:{lon_cell}:{requested}:{language}".encode()
            ).hexdigest()
        )
        cached = self.cache.get(key)
        if cached is None:
            selectors = "".join(
                f'nwr["{tag}"="{value}"]({bbox});'
                for k in requested
                for tag, value in category_tags(k)
            )
            for endpoint in endpoints:
                try:
                    data = await self.request(
                        "overpass",
                        {
                            "data": "[out:json][timeout:3];("
                            + selectors
                            + ");out center tags;"
                        },
                        endpoint,
                    )
                    if (
                        not isinstance(data, dict)
                        or not isinstance(data.get("elements"), list)
                        or data.get("remark")
                    ):
                        raise NearbyError("osm_incomplete_response")
                    if data["elements"]:
                        cached = {"elements": data["elements"], "provider": "overpass"}
                        break
                except NearbyError as exc:
                    self.failures.append("overpass:" + str(exc))
            if cached is not None:
                self.cache.put(key, cached, 900)
        # A successful but geographically unhelpful Overpass response should
        # still allow the next source. Public search is limited, not exhaustive.
        if cached is None or not cached.get("elements"):
            cached = await self.nominatim_candidates(
                origin, requested, radius, language
            )
        self.served_by = str(cached.get("provider", "overpass"))
        result = self.parse_candidates(cached, origin, requested, radius, now)
        if not result and self.served_by == "overpass":
            cached = await self.nominatim_candidates(
                origin, requested, radius, language
            )
            self.served_by = "nominatim"
            result = self.parse_candidates(cached, origin, requested, radius, now)
        return result

    def parse_candidates(
        self,
        cached: dict[str, Any],
        origin: Position,
        requested: list[str],
        radius: int,
        now: datetime,
    ) -> list[dict[str, Any]]:
        elements = cached.get("elements", [])
        if not isinstance(elements, list):
            raise NearbyError("osm_invalid_response")
        result = []
        for element in elements:
            try:
                if not isinstance(element, dict):
                    continue
                tags = element["tags"]
                if not isinstance(tags, dict) or element.get("type") not in {
                    "node",
                    "way",
                    "relation",
                }:
                    continue
                kind = next(
                    (
                        k
                        for k in requested
                        if any(
                            tags.get(tag) == value for tag, value in category_tags(k)
                        )
                    ),
                    None,
                )
                if not kind or not tags.get("name"):
                    continue
                if isinstance(tags.get("addr:city"), str):
                    self.area = tags["addr:city"][:100]
                point = element.get("center", element)
                lat, lon = float(point["lat"]), float(point["lon"])
                if (
                    not math.isfinite(lat)
                    or not math.isfinite(lon)
                    or not -90 <= lat <= 90
                    or not -180 <= lon <= 180
                ):
                    continue
                meters = distance(origin, lat, lon)
                if meters > radius:
                    continue
                address = ", ".join(
                    filter(
                        None,
                        (
                            " ".join(
                                filter(
                                    None,
                                    (
                                        tags.get("addr:housenumber"),
                                        tags.get("addr:street"),
                                    ),
                                )
                            ),
                            tags.get("addr:postcode"),
                            tags.get("addr:city"),
                        ),
                    )
                )
                result.append(
                    dict(
                        name=str(tags["name"])[:200],
                        category=kind,
                        distance_m=meters,
                        distance_kind="approximate straight-line",
                        address=address[:400],
                        country=str(tags.get("addr:country", ""))[:2].upper(),
                        website=tags.get("website", tags.get("contact:website", "")),
                        phone=tags.get("phone", tags.get("contact:phone", "")),
                        opening_hours=tags.get("opening_hours"),
                        opening=opening_status(
                            tags.get("opening_hours"), (lat, lon), now
                        ),
                        source=f"https://www.openstreetmap.org/{element['type']}/{int(element['id'])}",
                        provider=self.served_by,
                    )
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        return result

    async def nominatim_candidates(
        self, origin: Position, requested: list[str], radius: int, language: str
    ) -> dict[str, Any]:
        # Fixed shared cells: no original/rounded request position or identity
        # enters a key or cached result. Search only requested categories.
        cell_lat = (math.floor(origin.latitude * 50) + 0.5) / 50
        cell_lon = (math.floor(origin.longitude * 50) + 0.5) / 50
        pad = (math.ceil(radius / 1500) * 1500) / 110000
        lon_pad = pad / max(0.01, math.cos(math.radians(cell_lat)))
        box = (
            max(-180, cell_lon - 0.01 - lon_pad),
            min(90, cell_lat + 0.01 + pad),
            min(180, cell_lon + 0.01 + lon_pad),
            max(-90, cell_lat - 0.01 - pad),
        )
        endpoint = os.environ.get(
            "ATLAS_NOMINATIM_URL", "https://nominatim.openstreetmap.org/search"
        )
        elements: list[dict[str, Any]] = []
        seen: set[tuple[str, object]] = set()
        for kind in requested:
            term = CATEGORY_TERMS[kind].get(language, CATEGORY_TERMS[kind]["en"])
            # Nominatim returns at most NOMINATIM_LIMIT rows by relevance, not by
            # distance: in a dense town centre one search misses close places
            # (owner report 2026-11-13, Caen). A full answer splits the box in
            # four, within a fixed number of requests per category.
            boxes: list[tuple[tuple[float, float, float, float], int]] = [(box, 0)]
            requests = 0
            while boxes and requests < NOMINATIM_REQUESTS:
                current, depth = boxes.pop(0)
                cached, asked = await self.nominatim_box(
                    endpoint, current, term, kind, language
                )
                requests += asked
                if cached is None:
                    continue
                public_elements = cached.get("elements")
                if isinstance(public_elements, list):
                    for p in public_elements:
                        if not isinstance(p, dict):
                            continue
                        identity = (str(p.get("type")), p.get("id"))
                        if identity in seen:
                            continue
                        seen.add(identity)
                        elements.append(p)
                if cached.get("saturated") and depth < NOMINATIM_SPLIT_DEPTH:
                    west, north, east, south = current
                    middle_lon, middle_lat = (west + east) / 2, (north + south) / 2
                    boxes.extend(
                        (quadrant, depth + 1)
                        for quadrant in (
                            (west, north, middle_lon, middle_lat),
                            (middle_lon, north, east, middle_lat),
                            (west, middle_lat, middle_lon, south),
                            (middle_lon, middle_lat, east, south),
                        )
                    )
            if boxes:
                self.failures.append("nominatim:coverage_limited")
        return {"elements": elements, "provider": "nominatim"}

    async def nominatim_box(
        self,
        endpoint: str,
        box: tuple[float, float, float, float],
        term: str,
        kind: str,
        language: str,
    ) -> tuple[dict[str, Any] | None, int]:
        """One public box search (cached): (result or None, requests made)."""
        viewbox = ",".join(f"{v:.4f}" for v in box)
        key = (
            "public-pois:"
            + hashlib.sha256(
                f"{endpoint}:{viewbox}:{term}:{NOMINATIM_LIMIT}".encode()
            ).hexdigest()
        )
        cached = self.cache.get(key)
        if cached is not None:
            return cached, 0
        tag_pairs = category_tags(kind)
        try:
            rows = await self.request(
                "geocode",
                dict(
                    q=term,
                    format="jsonv2",
                    extratags="1",
                    addressdetails="1",
                    limit=str(NOMINATIM_LIMIT),
                    bounded="1",
                    viewbox=viewbox,
                    **{"accept-language": language},
                ),
            )
            if not isinstance(rows, list):
                raise NearbyError("osm_invalid_response")
            public = []
            for row in rows:
                if not isinstance(row, dict) or row.get("osm_type") not in {
                    "node",
                    "way",
                    "relation",
                }:
                    continue
                if (
                    row.get("category", row.get("class")),
                    row.get("type"),
                ) not in tag_pairs:
                    continue
                tags = row.get("extratags") or {}
                address = row.get("address") or {}
                if not isinstance(tags, dict) or not isinstance(address, dict):
                    continue
                tags = {
                    key: value
                    for key, value in tags.items()
                    if key
                    in {
                        "opening_hours",
                        "phone",
                        "website",
                        "contact:phone",
                        "contact:website",
                        "contact:housenumber",
                        "contact:street",
                        "contact:postcode",
                        "contact:city",
                    }
                    and isinstance(value, str)
                }
                tags["name"] = str(row.get("name", ""))
                tags[CATEGORIES[kind][0]] = CATEGORIES[kind][1]
                for key_part, value in (
                    ("housenumber", address.get("house_number")),
                    ("street", address.get("road")),
                    ("postcode", address.get("postcode")),
                    ("country", address.get("country_code")),
                    (
                        "city",
                        address.get(
                            "city", address.get("town", address.get("village"))
                        ),
                    ),
                ):
                    if isinstance(value, str):
                        tags["addr:" + key_part] = value
                if tags.get("addr:city"):
                    self.area = tags["addr:city"][:100]
                public.append(
                    dict(
                        type=row["osm_type"],
                        id=row.get("osm_id"),
                        lat=row.get("lat"),
                        lon=row.get("lon"),
                        tags=tags,
                    )
                )
            cached = {"elements": public, "saturated": len(rows) >= NOMINATIM_LIMIT}
            self.cache.put(key, cached, 900)
            return cached, 1
        except NearbyError as exc:
            self.failures.append("nominatim:" + str(exc))
            return None, 1


# Nominatim's own maximum per search; a full box is split in four, at most twice
# deep and NOMINATIM_REQUESTS searches per category (one per second by policy).
NOMINATIM_LIMIT = 40
NOMINATIM_SPLIT_DEPTH = 2
NOMINATIM_REQUESTS = 9

CATEGORY_TERMS = {
    "bakery": {
        "fr": "boulangerie",
        "en": "bakery",
        "de": "Bäckerei",
        "es": "panadería",
        "it": "panificio",
    },
    "convenience": {
        "fr": "supérette",
        "en": "convenience store",
        "de": "Lebensmittelgeschäft",
        "es": "tienda de conveniencia",
        "it": "minimarket",
    },
    "pharmacy": {
        "fr": "pharmacie",
        "en": "pharmacy",
        "de": "Apotheke",
        "es": "farmacia",
        "it": "farmacia",
    },
    "supermarket": {
        "fr": "supermarché",
        "en": "supermarket",
        "de": "Supermarkt",
        "es": "supermercado",
        "it": "supermercato",
    },
    "restaurant": {
        "fr": "restaurant",
        "en": "restaurant",
        "de": "Restaurant",
        "es": "restaurante",
        "it": "ristorante",
    },
    "cafe": {
        "fr": "café",
        "en": "cafe",
        "de": "Café",
        "es": "cafetería",
        "it": "caffè",
    },
    "fuel": {
        "fr": "station-service",
        "en": "petrol station",
        "de": "Tankstelle",
        "es": "gasolinera",
        "it": "benzinaio",
    },
}
