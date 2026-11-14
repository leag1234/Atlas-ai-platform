"""Deterministic calendar and clock arithmetic for the chat tools.

Dates use the proleptic Gregorian calendar of ``datetime``; time zones come from
the IANA database through ``zoneinfo``; public holidays come from the
``holidays`` package (MIT), imported on first use. Nothing here reads the
network, and every range is bounded before any loop runs.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from functools import cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

MAX_SPAN_DAYS = 36_600  # about one hundred years, for day-by-day counts
MAX_AMOUNT = {
    "minutes": 10_000_000,
    "hours": 1_000_000,
    "days": 400_000,
    "weeks": 50_000,
    "months": 12_000,
    "years": 1_000,
    "business_days": 20_000,
}
WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
DATETIME = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{1,2}:\d{2}(?::\d{2})?)")
TIME = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")
# "Here" when neither the call nor the browser names a zone.
DEFAULT_ZONE = "Europe/Paris"


class DateError(ValueError):
    """A precise, model-readable refusal: ``code: detail``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def parse_date(value: str | None, name: str = "date") -> date:
    text = (value or "").strip()
    if not DATE.fullmatch(text):
        raise DateError("invalid_date", f"{name} must be YYYY-MM-DD, got {text[:40]!r}")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise DateError("invalid_date", f"{text} is not a calendar date") from None


def parse_local(value: str | None, today: date) -> datetime:
    """A wall-clock time: YYYY-MM-DDTHH:MM[:SS], or HH:MM[:SS] on ``today``."""
    text = (value or "").strip()
    match = DATETIME.fullmatch(text)
    if match:
        day, clock = parse_date(match[1]), match[2]
    elif TIME.fullmatch(text):
        day, clock = today, text
    else:
        raise DateError(
            "invalid_time",
            f"use YYYY-MM-DDTHH:MM or HH:MM, got {text[:40]!r}",
        )
    parts = [int(part) for part in clock.split(":")]
    hour, minute, second = parts[0], parts[1], parts[2] if len(parts) > 2 else 0
    if hour > 23 or minute > 59 or second > 59:
        raise DateError("invalid_time", f"{clock} is not a time of day")
    return datetime(day.year, day.month, day.day, hour, minute, second)


@cache
def _zone_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for name in sorted(available_timezones()):
        index.setdefault(name.lower(), name)
    for name in sorted(available_timezones()):
        if "/" in name and not name.startswith(("Etc/", "SystemV/")):
            index.setdefault(name.rsplit("/", 1)[1].lower(), name)
    return index


def zone(value: str | None) -> ZoneInfo:
    """An IANA zone; a bare city of the database (``tokyo``) is accepted too."""
    text = (value or "").strip()
    if not text:
        raise DateError("missing_zone", "give an IANA time zone such as Europe/Paris")
    if text.upper() in {"UTC", "Z", "GMT"}:
        return ZoneInfo("UTC")
    key = re.sub(r"\s+", "_", text).lower()
    name = _zone_index().get(key)
    if name is None:
        raise DateError(
            "unknown_time_zone",
            f"{text[:64]!r} is not an IANA zone; use a name such as "
            "America/New_York or Asia/Tokyo",
        )
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise DateError("unknown_time_zone", text[:64]) from None


def _offset(moment: datetime) -> str:
    delta = moment.utcoffset() or timedelta(0)
    minutes = int(delta.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _moment(moment: datetime) -> dict[str, object]:
    dst = moment.dst()
    return {
        "local": moment.replace(tzinfo=None).isoformat(timespec="minutes"),
        "zone": str(moment.tzinfo),
        "utc_offset": _offset(moment),
        "abbreviation": moment.tzname(),
        "daylight_saving_time": bool(dst),
        "weekday": WEEKDAYS[moment.weekday()],
    }


def now(zone_name: str, clock: Callable[[], datetime]) -> dict[str, object]:
    place = zone(zone_name)
    moment = clock().astimezone(place)
    year, week, _ = moment.isocalendar()
    return {
        **_moment(moment),
        "date": moment.date().isoformat(),
        "time": moment.strftime("%H:%M"),
        "iso_week": f"{year}-W{week:02d}",
        "clock": "server clock",
    }


def resolve_local(local: datetime, place: ZoneInfo) -> list[datetime]:
    """Every instant showing this wall-clock time: none in a gap, two in an overlap."""
    found: list[datetime] = []
    for fold in (0, 1):
        candidate = local.replace(tzinfo=place, fold=fold)
        back = candidate.astimezone(timezone.utc).astimezone(place)
        if back.replace(tzinfo=None) == local and all(
            candidate.utcoffset() != other.utcoffset() for other in found
        ):
            found.append(candidate)
    return found


def convert_time(
    value: str | None,
    source: str,
    target: str | None,
    clock: Callable[[], datetime],
) -> dict[str, object]:
    origin, destination = zone(source), zone(target)
    local = parse_local(value, clock().astimezone(origin).date())
    instants = resolve_local(local, origin)
    if not instants:
        raise DateError(
            "nonexistent_local_time",
            f"{local.isoformat(timespec='minutes')} does not exist in {origin}: "
            "the clocks move forward over it (daylight saving time)",
        )
    results: list[dict[str, object]] = []
    for instant in instants:
        converted = instant.astimezone(destination)
        results.append(
            {
                "from": _moment(instant),
                "to": _moment(converted),
                "utc": instant.astimezone(timezone.utc).isoformat(timespec="minutes"),
                "day_change": (converted.date() - instant.date()).days,
            }
        )
    if len(results) == 1:
        return results[0]
    return {
        "ambiguous": True,
        "reason": f"{local.isoformat(timespec='minutes')} occurs twice in {origin} "
        "(the clocks move back); both readings are given",
        "readings": results,
    }


def add_months(day: date, months: int) -> tuple[date, bool]:
    """Calendar months; a day past the end of the target month is clamped."""
    years, month = divmod(day.month - 1 + months, 12)
    year = day.year + years
    if not 1 <= year <= 9999:
        raise DateError("date_out_of_range", "years 1 to 9999 only")
    last = calendar.monthrange(year, month + 1)[1]
    return date(year, month + 1, min(day.day, last)), day.day > last


def calendar_span(start: date, end: date) -> tuple[int, int, int]:
    """Completed years, months and days from start to a later or equal end."""
    months = (end.year - start.year) * 12 + end.month - start.month
    while months > 0 and add_months(start, months)[0] > end:
        months -= 1
    rest = (end - add_months(start, months)[0]).days
    return months // 12, months % 12, rest


def difference(first: str | None, second: str | None) -> dict[str, object]:
    start, end = parse_date(first), parse_date(second, "end_date")
    days = (end - start).days
    low, high = sorted((start, end))
    years, months, rest = calendar_span(low, high)
    sign = -1 if days < 0 else 1
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": days,
        "days_counting_both_ends": days + sign,
        "weeks": sign * (abs(days) // 7),
        "remaining_days_after_weeks": sign * (abs(days) % 7),
        "calendar": {"years": years, "months": months, "days": rest},
        "total_months": sign * (years * 12 + months),
        "direction": "end is before start" if days < 0 else "forward",
    }


def holiday_calendar(
    country: str, subdivision: str | None, years: range
) -> Mapping[date, str]:
    try:
        import holidays as calendars
    except ImportError:
        raise DateError(
            "holiday_calendar_unavailable",
            "the public holiday calendar is not installed on this server",
        ) from None
    code = country.strip().upper()
    if code not in calendars.list_supported_countries():
        raise DateError("unknown_country", f"no public holiday calendar for {code}")
    try:
        found: Mapping[date, str] = calendars.country_holidays(
            code, subdiv=subdivision or None, years=list(years)
        )
    except NotImplementedError:
        options = sorted(getattr(calendars.country_holidays(code), "subdivisions", ()))
        raise DateError(
            "unknown_subdivision",
            f"{subdivision!r} for {code}; known: {', '.join(options)[:300]}",
        ) from None
    return found


def day_info(
    value: str | None, country: str, subdivision: str | None
) -> dict[str, object]:
    day = parse_date(value)
    year, week, weekday = day.isocalendar()
    try:
        holidays = holiday_calendar(country, subdivision, range(day.year, day.year + 1))
        holiday: str | None = holidays.get(day)
    except DateError as exc:
        if exc.code != "holiday_calendar_unavailable":
            raise
        holiday = "unknown: the holiday calendar is not installed on this server"
    return {
        "date": day.isoformat(),
        "weekday": WEEKDAYS[day.weekday()],
        "iso_weekday": weekday,
        "iso_week": f"{year}-W{week:02d}",
        "day_of_year": day.timetuple().tm_yday,
        "days_in_year": 366 if calendar.isleap(day.year) else 365,
        "leap_year": calendar.isleap(day.year),
        "public_holiday": holiday,
        "holiday_calendar": _calendar_name(country, subdivision),
    }


def _calendar_name(country: str, subdivision: str | None) -> str:
    code = country.strip().upper()
    return f"{code}-{subdivision}" if subdivision else code


def business_days(
    first: str | None,
    second: str | None,
    country: str,
    subdivision: str | None,
    saturdays: bool,
) -> dict[str, object]:
    start, end = parse_date(first), parse_date(second, "end_date")
    low, high = sorted((start, end))
    span = (high - low).days + 1
    if span > MAX_SPAN_DAYS:
        raise DateError("range_too_long", f"{span} days; at most {MAX_SPAN_DAYS}")
    holidays = holiday_calendar(country, subdivision, range(low.year, high.year + 1))
    working = weekend = 0
    excluded: list[dict[str, str]] = []
    rest_days = {6} if saturdays else {5, 6}
    for offset in range(span):
        day = low + timedelta(days=offset)
        if day.weekday() in rest_days:
            weekend += 1
        elif day in holidays:
            excluded.append({"date": day.isoformat(), "name": holidays[day]})
        else:
            working += 1
    return {
        "start": low.isoformat(),
        "end": high.isoformat(),
        "business_days": working,
        "counted": "both ends included",
        "working_week": "Monday to Saturday" if saturdays else "Monday to Friday",
        "calendar_days": span,
        "rest_days": weekend,
        "public_holidays_excluded": excluded,
        "holiday_calendar": _calendar_name(country, subdivision),
    }


def add(
    value: str | None,
    amount: int | None,
    unit: str | None,
    zone_name: str,
    country: str,
    subdivision: str | None,
    clock: Callable[[], datetime],
) -> dict[str, object]:
    if amount is None or unit is None:
        raise DateError("missing_amount", "add needs amount and unit")
    if abs(amount) > MAX_AMOUNT[unit]:
        raise DateError("amount_too_large", f"at most {MAX_AMOUNT[unit]} {unit}")
    try:
        if unit in {"minutes", "hours"}:
            place = zone(zone_name)
            local = parse_local(value, clock().astimezone(place).date())
            instants = resolve_local(local, place)
            if not instants:
                raise DateError(
                    "nonexistent_local_time",
                    f"{local.isoformat(timespec='minutes')} does not exist in {place}",
                )
            step = timedelta(**{unit: amount})
            later = (instants[0].astimezone(timezone.utc) + step).astimezone(place)
            return {
                "start": _moment(instants[0]),
                "result": _moment(later),
                "elapsed": f"{amount} {unit} of real time (daylight saving applied)",
            }
        start = parse_date(value)
        clamped = False
        if unit == "business_days":
            return _add_business_days(start, amount, country, subdivision)
        if unit in {"days", "weeks"}:
            result = start + timedelta(days=amount * (7 if unit == "weeks" else 1))
        else:
            result, clamped = add_months(start, amount * (12 if unit == "years" else 1))
    except OverflowError:
        raise DateError("date_out_of_range", "years 1 to 9999 only") from None
    found: dict[str, object] = {
        "start": start.isoformat(),
        "result": result.isoformat(),
        "weekday": WEEKDAYS[result.weekday()],
    }
    if clamped:
        found["note"] = "the day does not exist in the target month: last day used"
    return found


def _add_business_days(
    start: date, amount: int, country: str, subdivision: str | None
) -> dict[str, object]:
    step = 1 if amount >= 0 else -1
    reach = abs(amount) // 200 + 2
    years = range(max(1, start.year - reach), min(9999, start.year + reach) + 1)
    holidays = holiday_calendar(country, subdivision, years)
    day, left, skipped = start, abs(amount), []
    while left:
        day += timedelta(days=step)
        if day.year not in years:
            raise DateError("range_too_long", "business day count too large")
        if day.weekday() >= 5:
            continue
        if day in holidays:
            skipped.append({"date": day.isoformat(), "name": holidays[day]})
            continue
        left -= 1
    return {
        "start": start.isoformat(),
        "result": day.isoformat(),
        "weekday": WEEKDAYS[day.weekday()],
        "counted": "the start day is not counted; weekends and public holidays skipped",
        "public_holidays_skipped": skipped,
        "holiday_calendar": _calendar_name(country, subdivision),
    }


def age(birth: str | None, on: str | None, today: date) -> dict[str, object]:
    born = parse_date(birth)
    if on:
        today = parse_date(on, "end_date")
    if today < born:
        raise DateError("birth_after_date", f"{born} is after {today}")
    years, months, days = calendar_span(born, today)
    upcoming, _ = add_months(born, (years + 1) * 12)
    found: dict[str, object] = {
        "birth_date": born.isoformat(),
        "on": today.isoformat(),
        "age_years": years,
        "and_months": months,
        "and_days": days,
        "next_birthday": upcoming.isoformat(),
        "days_until_next_birthday": (upcoming - today).days,
        "next_birthday_weekday": WEEKDAYS[upcoming.weekday()],
    }
    if born.month == 2 and born.day == 29:
        found["note"] = "29 February birthdays fall on 28 February in common years"
    return found


def holidays_of(
    year: int | None, country: str, subdivision: str | None
) -> dict[str, object]:
    if year is None:
        raise DateError("missing_year", "holidays needs year")
    found = holiday_calendar(country, subdivision, range(year, year + 1))
    return {
        "year": year,
        "holiday_calendar": _calendar_name(country, subdivision),
        "public_holidays": [
            {"date": day.isoformat(), "weekday": WEEKDAYS[day.weekday()], "name": name}
            for day, name in sorted(found.items())
        ],
    }


def run(
    arguments: Mapping[str, Any],
    clock: Callable[[], datetime],
    home_zone: str = DEFAULT_ZONE,
) -> dict[str, object]:
    """Dispatch one validated ``date_time`` call.

    ``home_zone`` is the person's browser zone when known: it stands for "here"
    and "today" whenever the call names no zone.
    """
    operation = arguments["operation"]
    country = str(arguments.get("country") or "FR")
    subdivision = arguments.get("subdivision")
    place = arguments.get("zone") or home_zone
    if operation == "now":
        return now(place, clock)
    if operation == "convert_time":
        return convert_time(
            arguments.get("date"), place, arguments.get("to_zone"), clock
        )
    if operation == "difference":
        return difference(arguments.get("date"), arguments.get("end_date"))
    if operation == "add":
        return add(
            arguments.get("date"),
            arguments.get("amount"),
            arguments.get("unit"),
            place,
            country,
            subdivision,
            clock,
        )
    if operation == "day_info":
        return day_info(arguments.get("date"), country, subdivision)
    if operation == "business_days":
        return business_days(
            arguments.get("date"),
            arguments.get("end_date"),
            country,
            subdivision,
            bool(arguments.get("saturdays")),
        )
    if operation == "age":
        today = clock().astimezone(zone(place)).date()
        return age(arguments.get("date"), arguments.get("end_date"), today)
    if operation == "holidays":
        return holidays_of(arguments.get("year"), country, subdivision)
    raise DateError("unknown_operation", str(operation)[:40])


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
