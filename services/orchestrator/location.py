"""REQ-NEARBY-01: ephemeral location boundary shared by Chat and Projects."""

from __future__ import annotations

from packages import attached_blocks

import math
import re
import unicodedata
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator


def normalized(text: str) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", text.lower())
        if not unicodedata.combining(c)
    )


CATEGORIES = {
    "bakery": (
        "shop",
        "bakery",
        r"\b(baker(?:y|ies)|boulanger\w*|pain|bread|backerei|panaderia|panificio)\b",
    ),
    "convenience": (
        "shop",
        "convenience",
        r"\b(convenience|superette\w*|epicerie\w*|lait|milk|alimentacion)\b",
    ),
    "pharmacy": (
        "amenity",
        "pharmacy",
        r"\b(pharmac\w*|medicament\w*|medicine|apotheke\w*|farmacia\w*)\b",
    ),
    "supermarket": (
        "shop",
        "supermarket",
        r"\b(supermarket\w*|supermarche\w*|supermercado\w*)\b",
    ),
    "restaurant": (
        "amenity",
        "restaurant",
        r"\b(restaurant\w*|restaurante\w*|ristorante\w*)\b",
    ),
    "cafe": ("amenity", "cafe", r"\b(cafe\w*|coffee)\b"),
    "fuel": (
        "amenity",
        "fuel",
        r"\b(fuel|petrol|gas station|station.service|essence)\b",
    ),
}


# Other OSM tags that mean the same kind of place. A bakery mapped only as a
# craft (craft=bakery, without shop=bakery) is still a place that sells bread.
ALTERNATIVE_TAGS: dict[str, tuple[tuple[str, str], ...]] = {
    "bakery": (("craft", "bakery"),),
}


def category_tags(kind: str) -> tuple[tuple[str, str], ...]:
    """Every (key, value) OSM tag that identifies a place of this category."""
    return ((CATEGORIES[kind][0], CATEGORIES[kind][1]), *ALTERNATIVE_TAGS.get(kind, ()))


def categories(text: str) -> list[str]:
    value = normalized(text)
    return [
        key for key, (_, _, pattern) in CATEGORIES.items() if re.search(pattern, value)
    ]


def location_dependent(text: str) -> bool:
    text = attached_blocks.BLOCKS.sub("", text)
    text = re.sub(r"^\[Attachment: [^\n]*\]$", "", text, flags=re.M)
    value = normalized(text)
    if re.search(
        r"\b(recette|recipe|translate|tradui\w*|definition|explain|explique|histoire)\b",
        value,
    ):
        return False
    nearby = r"\b(near(?:by|est)?|around me|close(?:st)?|pres|proche\w*|autour|quartier|a pied|dans le coin|cer­ca|cerca|vicino|nahe)\b"
    shopping = r"\b(open|ouvert\w*|ouverte\w*|abiert\w*|offen|aperto|maintenant|now|heure|tout de suite|ou aller)\b"
    return bool(
        categories(value) and (re.search(nearby, value) or re.search(shopping, value))
    )


def _plain(text: str) -> str:
    text = attached_blocks.BLOCKS.sub("", text)
    text = re.sub(r"^\[Attachment: [^\n]*\]$", "", text, flags=re.M)
    return " ".join(normalized(text).replace("'", " ").replace("’", " ").split())


# The device position, in the five interface languages, with common spellings
# ("geoloc", "localisation", "GPS", "where I am").
_POSITION = (
    r"(?:position\w*|positon|postion|posiiton|posicion\w*|posizione|localisation|"
    r"locali[sz]ation|location|locaiton|loaction|locatoin|"
    r"localizacion|localizzazione|geoloc\w*|gps|coordonnees|coordinates|"
    r"standort\w*|ubicacion|emplacement|ou je suis|ou je me trouve|"
    r"where i am|where am i|wo ich bin|donde estoy|dove sono)"
)
# A device that knows it, or a verb asking to read or share it.
_DEVICE = (
    r"(?:tel|tele|telephone|phone|portable|mobile|smartphone|iphone|android|"
    r"navigateur|browser|appareil|device|handy|movil|telefono|cellulare)"
)
_ASK = (
    r"(?:demand\w*|prend\w*|pren|utilis\w*|use|using|get|grab|ask|take|read|"
    r"check|regard\w*|cherch\w*|trouv\w*|recup\w*|activ\w*|share|partag\w*|"
    r"send|envo\w*|lis|lit|lire|nutz\w*|verwend\w*|frag\w*|usa\w*|prendi|"
    r"pide|chiedi|toma|leggi)"
)
_ME = r"(?:ma|mon|mes|my|meine?n?|mi|mia)"
_OWNER = r"(?:ma|mon|mes|my|la|le|the|your|votre|ta|ton|tu|meine?n?|mi|mia|su|sua|il)"
_UNAMBIGUOUS = (
    r"(?:location|locaiton|loaction|localisation|locali[sz]ation|geoloc\w*|gps|"
    r"standort\w*|ubicacion|localizacion|localizzazione|coordonnees|coordinates)"
)
_LOCATE_ME = (
    r"\b(?:localise[sz]?|geolocalise[sz]?|locate|find|trouve|ortet?|localiza|"
    r"localizza)(?:\s|-)*(?:moi|me|mich|mi)\b"
)
# Questions about how location works, not a request to use it.
_EXPLAIN = (
    r"\b(?:explain|explique\w*|how does|how do \w+ work|comment (?:ca )?marche|"
    r"comment fonctionne|what is|qu est ce que|c est quoi le gps|wie funktioniert)\b"
)
# How-to questions about some application's own sharing feature.
_HOW_TO = (
    r"\b(?:comment|how (?:do|to|can)|wie kann|como se|come si)\b(?:\s+\w+){0,3}?\s+"
    r"(?:partag|share|envo|send|activ|desactiv|turn|teil|compart|condivid)"
)


def device_position_request(text: str) -> bool:
    """The message asks the assistant to read or ask for the device position.

    "demande ma position au tel", "use my location", "prends ma position GPS",
    "localise-moi", "where am I": the location consent flow answers it, never a
    text reply saying the position cannot be read.
    """
    value = _plain(text)
    if not value or re.search(_HOW_TO, value) or re.search(_EXPLAIN, value):
        return False
    if re.search(_LOCATE_ME, value):
        return True
    position = re.search(rf"\b{_POSITION}\b", value)
    if not position:
        return False
    if re.search(
        r"\b(?:where am i|ou suis je|ou je suis|donde estoy|dove sono|wo bin ich)\b",
        value,
    ):
        return True
    # "prendre position" (take a stance) and "my position on this" are not the
    # device: a verb needs an owner word before the position, and an owner word
    # alone needs an unambiguous term (location, GPS, geoloc).
    return bool(
        re.search(rf"\b{_DEVICE}\b", value)
        or re.search(
            rf"\b{_ASK}\b(?:\s+\w+){{0,2}}?\s+{_OWNER}\s+(?:\w+\s+)?{_POSITION}\b",
            value,
        )
        or re.search(rf"\b{_ME}\s+(?:\w+\s+)?{_UNAMBIGUOUS}\b", value)
    )


# Requests that depend on where the person is, beyond nearby shops: travel
# times, routes, departures "from here", "from home", the weather here, places
# near me. Used when no routing decision exists (router off or failed).
_FROM_HERE = (
    r"\b(?:from (?:here|home|my place|where i am)|d ici|depuis ici|de chez moi|"
    r"depuis chez moi|depuis la maison|de la maison|d ou je suis|depuis ma position|"
    r"von hier|von zu hause|desde aqui|desde casa|da qui|da casa)\b"
)
_NEAR_ME = (
    r"\b(?:near me|nearby|around me|close to me|around here|pres de (?:moi|chez moi|ici)|"
    r"autour de (?:moi|chez moi|ici)|pas loin|dans le coin|a proximite|dans les environs|"
    r"in der nahe|cerca de mi|vicino a me|qui vicino)\b"
)
_HERE_NOW = (
    r"\b(?:(?:meteo|temps|weather|wetter|tiempo|tempo|pluie|rain|pleuvoir|pleut)\b"
    r"(?:\s+\w+){0,4}\s+(?:ici|here|hier|aqui|qui|chez moi|outside|dehors)|"
    r"(?:il fait|it is|it s)\s+(?:quel temps|beau|froid|chaud|how)\s+(?:ici|here|dehors|outside))\b"
)
_TRAVEL = (
    r"\b(?:itinerai\w*|directions?|route to|how (?:do i|can i|to) get (?:to|there)|"
    r"comment (?:aller|me rendre|y aller|je vais|on va|rejoindre)|"
    r"combien de temps (?:(?:faut il )?pour (?:aller|me rendre|y aller|arriver|rejoindre|venir)|a pied|en voiture|en bus|en velo|en train|de trajet|de route)|"
    r"how long (?:to (?:get|walk|drive)|does it take|would it take)|a quelle distance|"
    r"how far (?:is|am i)|prochain (?:bus|train|tram|metro|car)|next (?:bus|train|tram|metro)|"
    r"wie komme ich|como llego|come arrivo)\b"
)


def position_dependent(text: str) -> bool:
    """The answer depends on the person's position (lexical, any category)."""
    if location_dependent(text):
        return True
    value = _plain(text)
    if re.search(
        r"\b(?:recette|recipe|translate|tradui\w*|definition|explain|explique)\b",
        value,
    ):
        return False
    return bool(
        re.search(_FROM_HERE, value)
        or re.search(_NEAR_ME, value)
        or re.search(_HERE_NOW, value)
        or re.search(_TRAVEL, value)
        or device_position_request(text)
    )


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90, repr=False)
    longitude: float = Field(ge=-180, le=180, repr=False)

    @model_validator(mode="after")
    def minimize(self) -> Self:
        self.latitude = round(self.latitude, 3)
        step = 0.001 / max(0.01, math.cos(math.radians(self.latitude)))
        self.longitude = max(-180, min(180, round(self.longitude / step) * step))
        return self


# Beyond this uncertainty a browser position is not used silently: the reply
# says so and asks for an address (owner report 2026-11-13, a desktop browser
# located the person at their provider's network).
COARSE_ACCURACY_M = 1000


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    consent: Literal["once", "always", "refused", "denied"]
    position: Position | None = Field(default=None, repr=False, exclude=True)
    place: str | None = Field(
        default=None, min_length=2, max_length=200, repr=False, exclude=True
    )
    timezone: str | None = Field(default=None, max_length=80)
    # Radius in metres of the browser's own uncertainty (Geolocation API
    # coords.accuracy). A large radius means a network or IP estimate, which
    # often places the person at their access provider rather than where they are.
    accuracy: int | None = Field(default=None, ge=0, le=40_000_000, exclude=True)
    # Why the device gave no position (consent "denied"): the browser has no
    # geolocation, the site permission is refused, no fix, or no fix in time.
    # The reply says which instead of a generic "cannot access your position".
    failure: Literal["unsupported", "permission", "unavailable", "timeout"] | None = (
        None
    )

    @property
    def coarse(self) -> bool:
        """A browser fix too imprecise to say which shops are around the person."""
        return (
            self.position is not None
            and self.accuracy is not None
            and self.accuracy > COARSE_ACCURACY_M
        )

    @model_validator(mode="after")
    def validate_consent(self) -> Self:
        if self.position is not None and (
            self.consent in {"refused", "denied"} or self.place
        ):
            raise ValueError("location_consent_conflict")
        if self.failure is not None and self.consent != "denied":
            raise ValueError("location_consent_conflict")
        if self.timezone:
            try:
                ZoneInfo(self.timezone)
            except ZoneInfoNotFoundError:
                raise ValueError("location_timezone_invalid") from None
        return self


LEDGER_LABEL = "location used: approximate, this request"


# Open WebUI persists its own request bodies. Only this random, single-use
# capability passes through it; the location stays in Atlas memory for <=120 s.
# This deployment has one adapter worker. Multiple workers need sticky routing
# or an equivalent shared ephemeral store, never a durable request cache.
class LocationTickets:
    def __init__(self) -> None:
        self.values: dict[str, tuple[Location, object]] = {}

    def issue(self, location: Location) -> str:
        import asyncio
        from uuid import uuid4

        if len(self.values) >= 256:
            raise ValueError("location_capacity")
        token = str(uuid4())
        expiry = asyncio.get_running_loop().call_later(120, self.take, token)
        self.values[token] = (location, expiry)
        return token

    def take(self, token: str) -> Location | None:
        import asyncio

        entry = self.values.pop(token, None)
        if entry is None:
            return None
        location, expiry = entry
        if isinstance(expiry, asyncio.TimerHandle):
            expiry.cancel()
        return location


location_tickets = LocationTickets()
