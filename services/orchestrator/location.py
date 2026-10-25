"""REQ-NEARBY-01: ephemeral location boundary shared by Chat and Projects."""

from __future__ import annotations

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


def categories(text: str) -> list[str]:
    value = normalized(text)
    return [
        key for key, (_, _, pattern) in CATEGORIES.items() if re.search(pattern, value)
    ]


def location_dependent(text: str) -> bool:
    text = re.sub(r"<attached_files>.*?</attached_files>", "", text, flags=re.S)
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


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    consent: Literal["once", "always", "refused", "denied"]
    position: Position | None = Field(default=None, repr=False, exclude=True)
    place: str | None = Field(
        default=None, min_length=2, max_length=200, repr=False, exclude=True
    )
    timezone: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def validate_consent(self) -> Self:
        if self.position is not None and (
            self.consent in {"refused", "denied"} or self.place
        ):
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
