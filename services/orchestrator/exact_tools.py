"""Dates, times, units and currencies computed by code; tool contracts live in code.

The model decides what to compute; deterministic code computes it (roadmap T8).
The three tools are offered on turns whose question is about one of these
subjects, like the visual tools: every other turn keeps the request it has
always sent, so recorded provider exchanges stay valid.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from packages.privacy import StrictPrivacyUnavailable
from packages.validation import describe_validation
from services.orchestrator import datecalc, fx_rates, units
from services.orchestrator.loop import Message


class DateTime(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal[
        "now",
        "convert_time",
        "difference",
        "add",
        "day_info",
        "business_days",
        "age",
        "holidays",
    ]
    date: str | None = Field(default=None, max_length=40)
    end_date: str | None = Field(default=None, max_length=40)
    amount: int | None = Field(default=None, ge=-10_000_000, le=10_000_000)
    unit: (
        Literal["minutes", "hours", "days", "weeks", "months", "years", "business_days"]
        | None
    ) = None
    zone: str | None = Field(default=None, max_length=64)
    to_zone: str | None = Field(default=None, max_length=64)
    country: str = Field(default="FR", pattern=r"^[A-Za-z]{2}$")
    subdivision: str | None = Field(default=None, max_length=12)
    saturdays: bool = False
    year: int | None = Field(default=None, ge=1900, le=2200)


class UnitConvert(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: float = Field(ge=-1e15, le=1e15)
    from_unit: str = Field(min_length=1, max_length=40)
    to_unit: str = Field(min_length=1, max_length=40)


class CurrencyConvert(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    amount: float = Field(ge=0, le=1e12)
    from_currency: str = Field(pattern=r"^[A-Za-z]{3}$")
    to_currency: str = Field(pattern=r"^[A-Za-z]{3}$")


SCHEMAS: dict[str, type[BaseModel]] = {
    "date_time": DateTime,
    "unit_convert": UnitConvert,
    "currency_convert": CurrencyConvert,
}
DESCRIPTIONS = {
    "date_time": (
        "Exact calendar and clock computation, done by code. Use it for every "
        "weekday, date difference, date plus or minus a duration, business-day "
        "count, age, ISO week, public holiday, current time and time-zone "
        "conversion: never work these out yourself. Operations: now (current "
        "date and time in zone, server clock); convert_time (date as "
        "YYYY-MM-DDTHH:MM, or HH:MM for today, from zone to to_zone, daylight "
        "saving applied); difference (date to end_date: days, weeks, calendar "
        "years, months and days); add (date plus amount of unit, negative to "
        "subtract; minutes and hours need a time in date); day_info (weekday, "
        "ISO week, day of year and public holiday of date); business_days "
        "(working days from date to end_date, both included, weekends and "
        "public holidays excluded; saturdays=true counts Saturdays, French "
        "jours ouvrables); age (completed years from the birth date in date to "
        "end_date or today); holidays (public holidays of year). Dates are "
        "YYYY-MM-DD. zone and to_zone are IANA names such as Europe/Paris or "
        "America/New_York; without zone the user's own zone is used. country "
        "is ISO 3166 alpha-2 (default FR); subdivision for regional holidays, "
        "e.g. 57 Moselle, 6AE Alsace, 974 La Reunion."
    ),
    "unit_convert": (
        "Exact unit conversion, done by code: never convert units yourself. "
        "Length, mass, volume and cooking measures, area, speed, temperature, "
        "energy, power, pressure, data sizes, durations and fuel consumption "
        "(L/100 km, mpg). Pass the number and the unit names or symbols as the "
        "user wrote them (km, mi, lb, oz, cup, tbsp, cuillere a soupe, degF, "
        "kWh, GB, GiB, mph, L/100 km). gallon, pint, cup and fl oz are US "
        "measures; use imperial_gallon, imperial_pint or metric_cup otherwise. "
        "Different dimensions are refused with an error. Calendar months and "
        "years belong to date_time."
    ),
    "currency_convert": (
        "Convert an amount between currencies with the European Central Bank "
        "daily euro reference rates, fetched by code. Returns the result, the "
        "rate, its date and the source URL: give the rate date and cite the "
        "source. Currencies are ISO 4217 codes (EUR, USD, GBP, CHF, JPY...). "
        "When the user supplies their own rate, use calculator with that rate "
        "instead. If the rates cannot be fetched, say so: never estimate a rate."
    ),
}
FAMILIES = {"date": "date_time", "unit": "unit_convert", "currency": "currency_convert"}


def declarations(families: Sequence[str] | frozenset[str]) -> list[Message]:
    return [
        {
            "type": "function",
            "function": {
                "name": FAMILIES[family],
                "description": DESCRIPTIONS[FAMILIES[family]],
                "parameters": SCHEMAS[FAMILIES[family]].model_json_schema(),
            },
        }
        for family in ("date", "unit", "currency")
        if family in families
    ]


async def execute(
    name: str, arguments: str, timeout: float, home_zone: str = datecalc.DEFAULT_ZONE
) -> Message:
    schema = SCHEMAS[name]
    try:
        request = schema.model_validate_json(arguments)
        if isinstance(request, DateTime):
            data = datecalc.run(request.model_dump(), datecalc.utc_now, home_zone)
        elif isinstance(request, UnitConvert):
            data = units.convert(
                Decimal(str(request.value)), request.from_unit, request.to_unit
            )
        else:
            assert isinstance(request, CurrencyConvert)
            rates = await fx_rates.BOOK.rates(timeout)
            data = fx_rates.convert(
                Decimal(str(request.amount)),
                request.from_currency,
                request.to_currency,
                rates,
            )
    except ValidationError as exc:
        return {
            "error": "invalid_arguments",
            "message": describe_validation(exc),
            "schema": schema.model_json_schema(),
        }
    except (datecalc.DateError, units.UnitError, fx_rates.RatesError) as exc:
        return {"error": exc.code, "message": str(exc)}
    except StrictPrivacyUnavailable as exc:
        return {"error": exc.code, "message": exc.message}
    return {"data": data}


# --- Relevance: which of the three subjects a question is about. ------------
# Patterns run on lower-case text without accents, in the five UI languages.

_MONTH = (
    r"(?:janv(?:ier)?|fevr?(?:ier)?|mars|avr(?:il)?|mai|juin|juil(?:let)?|aout|"
    r"sept(?:embre)?|oct(?:obre)?|nov(?:embre)?|dec(?:embre)?|jan(?:uary)?|"
    r"feb(?:ruary)?|march|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:tember)?|"
    r"october|november|december|januar|februar|marz|juni|juli|oktober|dezember|"
    r"enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|"
    r"noviembre|diciembre|gennaio|febbraio|aprile|maggio|giugno|luglio|"
    r"settembre|ottobre|dicembre)"
)
_DATE = re.compile(
    r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b"
    rf"|\b\d{{1,2}}(?:er|st|nd|rd|th|\.)?\s+(?:de\s+)?{_MONTH}\b"
    rf"|\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?\b"
)
_PERIOD = (
    r"(?:jours?|semaines?|mois|ans|annees?|heures?|minutes?|days?|weeks?|months?|"
    r"years?|hours?|tage?n?|wochen?|monate?n?|jahre?n?|stunden?|dias?|semanas?|"
    r"meses|anos|horas?|giorni|settimane|mesi|anni|ore)"
)
_DATE_CUES = re.compile(
    "|".join(
        (
            rf"\b(?:combien|how many|nombre|number|wie viele|cuantos|cuantas|quanti|quante)\s+(?:de\s+|d'|of\s+)?{_PERIOD}\b",
            r"\bjours?\s+(?:ouvres?|ouvrables?|feries?)\b|\bferies?\b",
            r"\b(?:business|working|work)\s+days?\b|\b(?:public|bank)\s+holidays?\b",
            r"\b(?:feiertage?|arbeitstage?|werktage?)\b|\bdias?\s+(?:habiles|laborables|festivos)\b",
            r"\bgiorni\s+(?:lavorativi|festivi)\b",
            r"\bquel(?:le)?s?\s+jours?\b|\b(?:what|which)\s+day\b|\bday of (?:the )?week\b",
            r"\bjour de (?:la )?semaine\b|\bwochentag\b|\bque dia\b|\bche giorno\b",
            r"\btombe(?:ra|rait)?\b|\bfalls? on\b|\bwelcher tag\b",
            r"\bsemaine\s+(?:iso|n|no|numero)\b|\b(?:numero|n|no)\s+de\s+(?:la\s+)?semaine\b",
            r"\bweek\s+(?:number|no|nr)\b|\biso\s+week\b|\bkalenderwoche\b",
            r"\bquel\s+age\b|\bhow\s+old\b|\bwie\s+alt\b|\bque\s+edad\b|\bquanti\s+anni\b",
            r"\bquelle\s+heure\b|\bwhat\s+time\b|\bwie\s+spat\b|\buhrzeit\b|\bque\s+hora\b|\bche\s+ore\b",
            r"\bheure\s+(?:a|au|aux|en|de|locale|actuelle)\b|\bfuseaux?\s+horaires?\b",
            r"\btime\s?zones?\b|\bdecalage\s+horaire\b|\btime\s+difference\b|\bjet\s?lag\b|\bzeitzone\b",
            r"\b(?:utc|gmt)\b|\b\d{1,2}(?::\d{2})?\s*(?:h|am|pm)?\s*(?:cet|cest|pst|pdt|est|edt|bst|jst|ist)\b",
            rf"\b(?:dans|in|il y a|ago|d'ici|apres|after|avant|before|plus|moins|minus|en)\s+\d+\s+{_PERIOD}\b",
            rf"\b\d+\s+{_PERIOD}\s+(?:apres|after|avant|before|from|later|plus tard|ago|de plus|de moins)\b",
            r"\bhow\s+long\s+(?:until|till|before|since|ago)\b|\bdepuis\s+(?:combien|quand)\b",
            r"\bcombien\s+de\s+temps\s+(?:reste|jusqu|avant|depuis|entre|il\s+reste)\b",
            r"\b(?:date|jour)\s+(?:limite|d'echeance|butoir)\b",
            r"\bannee\s+bissextile\b|\bleap\s+year\b",
        )
    )
)
_UNIT = (
    r"(?:km|kms|kilometres?|kilometers?|miles?|mi|m|metres?|meters?|cm|mm|"
    r"inch(?:es)?|pouces?|ft|feet|foot|pieds?|yards?|yd|kg|kilos?|grammes?|grams?|g|"
    r"lbs?|pounds?|livres?|oz|onces?|ounces?|stones?|tonnes?|tons?|l|litres?|liters?|"
    r"ml|cl|dl|gallons?|gal|pints?|pintes?|quarts?|qt|fl\.? ?oz|cups?|tasses?|tbsp|"
    r"tsp|cuilleres?\s+a\s+(?:soupe|cafe)|tablespoons?|teaspoons?|m2|km2|hectares?|"
    r"ha|acres?|sq\.? ?ft|square\s+(?:feet|foot|meters?|metres?|miles?)|mph|km/?h|"
    r"kmh|noeuds?|knots?|m/s|°\s?[cf]|degres?|degrees?|celsius|fahrenheit|kelvin|kwh|"
    r"wh|joules?|kj|kcal|calories?|btu|chevaux|cv|hp|horsepower|bars?|psi|atm|hpa|"
    r"go|gb|mo|mb|to|tb|ko|kb|gio|gib|mio|mib|octets?|bytes?|bits?|mpg|"
    r"l/100\s?km|barils?|barrels?)"
)
_NUMBER = r"\d+(?:[.,]\d+)?"
_U = rf"(?<!\w){_UNIT}(?!\w)"
# Without a number in front, short symbols that are also words (to, go, m, l)
# would match ordinary sentences: a bare unit must be one of these.
_BARE_UNIT = (
    r"(?:km|kilometres?|kilometers?|miles?|metres?|meters?|cm|mm|inch(?:es)?|"
    r"pouces?|feet|foot|pieds?|yards?|kg|kilos?|grammes?|grams?|lbs?|pounds?|"
    r"livres?|oz|onces?|ounces?|stones?|tonnes?|litres?|liters?|ml|cl|gallons?|"
    r"pints?|pintes?|quarts?|fl\.? ?oz|cups?|tasses?|tbsp|tsp|cuilleres?\s+a\s+"
    r"(?:soupe|cafe)|tablespoons?|teaspoons?|m2|km2|hectares?|acres?|sq\.? ?ft|"
    r"square\s+(?:feet|foot|meters?|metres?|miles?)|mph|km/?h|knots?|noeuds?|"
    r"°\s?[cf]|degres?|degrees?|celsius|fahrenheit|kelvin|kwh|joules?|kcal|"
    r"calories?|btu|horsepower|psi|gb|mb|tb|gib|mib|octets?|bytes?|mpg|l/100\s?km)"
)
_UB = rf"(?<!\w){_BARE_UNIT}(?!\w)"
_CONVERT = (
    r"\b(?:convert|converts|converted|converting|conversion|convertir|convertis|"
    r"convertit|convertissez|converti|convertie|convierte|convertire|umrechnen|"
    r"umrechnung|rechne)\b"
)
_UNIT_CUES = re.compile(
    "|".join(
        (
            rf"{_NUMBER}\s*{_UNIT}(?!\w).{{0,40}}\b(?:en|in|into|to|vers|a|dans)\s+(?:\w+\s+)?{_U}",
            rf"{_CONVERT}.{{0,60}}{_UB}",
            rf"\b(?:how many|how much|combien|wie viele?|cuant[oa]s?|quant[ioe])\s+(?:de\s+|d')?{_UB}.{{0,40}}\b(?:in|dans|en|per|par|pour|make|font|fait|y a)\b",
            rf"{_NUMBER}\s*{_UNIT}(?!\w).{{0,30}}(?:combien|how many|how much|ca fait|c'est|=|equal|equivaut|en metrique|in metric|en imperial|in imperial)",
            r"\d\s*°\s?[cf]\b",
        )
    )
)
_CURRENCY = (
    r"(?:eur|euros?|usd|dollars?|gbp|sterling|pounds?|chf|francs?\s+suisses?|"
    r"swiss\s+francs?|jpy|yens?|cny|yuans?|renminbi|rmb|cad|aud|sek|nok|dkk|couronnes?|"
    r"kronor|kroner|krona|krone|pln|zlotys?|czk|huf|forints?|lei|liras?|inr|"
    r"roupies?|rupees?|brl|reais|mxn|pesos?|krw|hkd|sgd|nzd|zar|rands?|isk|"
    r"shekels?|thb|bahts?|idr|myr|ringgits?|bgn|bucks|balles)"
)
_SYMBOL = r"[€$£¥]"
_FX_CUES = re.compile(
    "|".join(
        (
            r"\btaux\s+de\s+change\b|\bexchange\s+rates?\b|\bwechselkurs\b|\btipo\s+de\s+cambio\b",
            r"\btasso\s+di\s+cambio\b|\bforex\b|\bdevises?\b",
            r"\bcours\s+(?:de\s+l'|du\s+|de\s+la\s+)?(?:euro|dollar|livre|franc|yen|yuan)\b",
            rf"(?:\b{_CURRENCY}\b|{_SYMBOL}).{{0,50}}\b(?:en|in|into|to|vers|a|contre|against)\s+(?:\w+\s+)?(?:\b{_CURRENCY}\b|{_SYMBOL})",
            rf"(?:{_CONVERT}|\b(?:changer|change|exchange|cambiar|cambiare)\b).{{0,60}}(?:\b{_CURRENCY}\b|{_SYMBOL})",
            rf"(?:\b{_CURRENCY}\b|{_SYMBOL}).{{0,30}}\b(?:vaut|valent|worth|wert|vale|valgono)\b",
        )
    )
)


def _plain(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.lower().replace("’", "'"))
    return "".join(c for c in folded if not unicodedata.combining(c))


def relevant(question: str, previous: str = "") -> frozenset[str]:
    """Subjects of the question; a short follow-up inherits the previous one's."""
    text = _plain(question[:4000])
    found: set[str] = set()
    if _DATE_CUES.search(text) or len(_DATE.findall(text)) >= 2:
        found.add("date")
    if _UNIT_CUES.search(text):
        found.add("unit")
    if _FX_CUES.search(text):
        found.add("currency")
    if not found and previous and len(text) <= 120:
        found = set(relevant(previous))
    return frozenset(found)
