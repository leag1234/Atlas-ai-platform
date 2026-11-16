"""iCalendar (RFC 5545) files for reminders, written by code without a dependency.

One VEVENT in its IANA time zone, with a VTIMEZONE built from the same zoneinfo
data, a DISPLAY alarm at the event time and an optional earlier one. Phones'
calendars ring on the alarms. The survey and the choice of writing the file here
rather than adding a package are in docs/25-reminders.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
# Years of zone transitions read to describe a time zone; a rule found over all of
# them becomes an RRULE that holds for every later year.
SCAN_YEARS = 12
EVENT_MINUTES = 15


@dataclass(frozen=True)
class Recurrence:
    """FREQ, INTERVAL, BYDAY (weekday numbers, Monday=0) and BYMONTHDAY."""

    freq: str
    interval: int = 1
    byday: tuple[int, ...] = ()
    bymonthday: int | None = None

    def rule(self) -> str:
        parts = ["FREQ=" + self.freq]
        if self.interval > 1:
            parts.append(f"INTERVAL={self.interval}")
        if self.byday:
            parts.append("BYDAY=" + ",".join(WEEKDAY_CODES[day] for day in self.byday))
        if self.bymonthday is not None:
            parts.append(f"BYMONTHDAY={self.bymonthday}")
        return ";".join(parts)


def escape_text(value: str) -> str:
    """TEXT value escaping (RFC 5545 3.3.11)."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold(line: str) -> str:
    """Lines of at most 75 octets, continued with CRLF and a space (3.1).

    A UTF-8 character is never split between two lines.
    """
    out: list[str] = []
    current, size, limit = "", 0, 75
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            out.append(current)
            current, size, limit = " ", 1, 75
        current += char
        size += width
    out.append(current)
    return "\r\n".join(out)


def _offset(delta: timedelta | None) -> str:
    seconds = int((delta or timedelta(0)).total_seconds())
    sign = "-" if seconds < 0 else "+"
    seconds = abs(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{sign}{hours:02d}{minutes:02d}" + (f"{secs:02d}" if secs else "")


def _local(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S")


@dataclass(frozen=True)
class Transition:
    utc: datetime
    before: timedelta
    after: timedelta
    name: str
    daylight: bool

    @property
    def onset(self) -> datetime:
        """Local wall time of the onset, read in the offset in force before it."""
        return (self.utc + self.before).replace(tzinfo=None)


def _offset_at(zone: ZoneInfo, instant: datetime) -> timedelta:
    return instant.astimezone(zone).utcoffset() or timedelta(0)


def transitions(zone: ZoneInfo, first: int, last: int) -> list[Transition]:
    """Offset changes of a zone from 1 January of first to 31 December of last."""
    found: list[Transition] = []
    day = datetime(first, 1, 1, tzinfo=timezone.utc)
    end = datetime(last + 1, 1, 1, tzinfo=timezone.utc)
    previous = _offset_at(zone, day)
    while day < end:
        following = day + timedelta(days=1)
        offset = _offset_at(zone, following)
        if offset != previous:
            low, high = day, following
            while high - low > timedelta(minutes=1):
                middle = low + (high - low) / 2
                if _offset_at(zone, middle) == previous:
                    low = middle
                else:
                    high = middle
            moment = high.replace(second=0, microsecond=0)
            local = moment.astimezone(zone)
            found.append(
                Transition(
                    moment,
                    previous,
                    offset,
                    local.tzname() or "",
                    bool(local.dst()),
                )
            )
            previous = offset
        day = following
    return found


def _yearly_rule(items: Sequence[Transition]) -> str | None:
    """BYMONTH and BYDAY shared by every onset of one kind, if any."""
    if len(items) < 2:
        return None
    onsets = [item.onset for item in items]
    years = [onset.year for onset in onsets]
    if years != list(range(years[0], years[0] + len(years))):
        return None
    if len({(o.month, o.weekday(), o.time()) for o in onsets}) != 1:
        return None
    if len({(item.before, item.after) for item in items}) != 1:
        return None
    from calendar import monthrange

    ordinal = {(o.day - 1) // 7 + 1 for o in onsets}
    last = {o.day + 7 > monthrange(o.year, o.month)[1] for o in onsets}
    code = WEEKDAY_CODES[onsets[0].weekday()]
    if last == {True}:
        position = "-1"
    elif len(ordinal) == 1:
        position = str(ordinal.pop())
    else:
        return None
    return f"FREQ=YEARLY;BYMONTH={onsets[0].month};BYDAY={position}{code}"


def vtimezone(zone_name: str, start: datetime) -> list[str]:
    """VTIMEZONE of an IANA zone around the event year (3.6.5)."""
    zone = ZoneInfo(zone_name)
    lines = ["BEGIN:VTIMEZONE", "TZID:" + zone_name, "X-LIC-LOCATION:" + zone_name]
    found = transitions(zone, start.year - 1, start.year + SCAN_YEARS - 1)
    if not found:
        moment = start.astimezone(zone)
        offset = _offset(moment.utcoffset())
        lines += [
            "BEGIN:STANDARD",
            "DTSTART:19700101T000000",
            "TZOFFSETFROM:" + offset,
            "TZOFFSETTO:" + offset,
            "TZNAME:" + (moment.tzname() or zone_name),
            "END:STANDARD",
        ]
        return [*lines, "END:VTIMEZONE"]
    for daylight in (True, False):
        kind = [item for item in found if item.daylight == daylight]
        if not kind:
            continue
        rule = _yearly_rule(kind)
        groups = [kind] if rule else [[item] for item in kind]
        for group in groups:
            first = group[0]
            component = "DAYLIGHT" if daylight else "STANDARD"
            lines += [
                "BEGIN:" + component,
                "DTSTART:" + _local(first.onset),
                "TZOFFSETFROM:" + _offset(first.before),
                "TZOFFSETTO:" + _offset(first.after),
            ]
            if first.name:
                lines.append("TZNAME:" + first.name)
            if rule:
                lines.append("RRULE:" + rule)
            lines.append("END:" + component)
    return [*lines, "END:VTIMEZONE"]


def _duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes % 1440 == 0:
        return f"P{minutes // 1440}D"
    if minutes % 60 == 0:
        return f"PT{minutes // 60}H"
    return f"PT{minutes}M"


def calendar_file(
    title: str,
    start: datetime,
    zone_name: str,
    *,
    early: timedelta | None = None,
    recurrence: Recurrence | None = None,
    now: datetime | None = None,
    uid: str | None = None,
) -> bytes:
    """The .ics bytes of one reminder; start is an aware datetime in zone_name."""
    if start.tzinfo is None:
        raise ValueError("reminder_start_naive")
    zone = ZoneInfo(zone_name)
    local = start.astimezone(zone)
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    summary = escape_text(title)
    event = [
        "BEGIN:VEVENT",
        "UID:" + (uid or uuid4().hex + "@atlas-reminder"),
        "DTSTAMP:" + stamp.strftime("%Y%m%dT%H%M%SZ"),
        f"DTSTART;TZID={zone_name}:" + _local(local),
        f"DURATION:PT{EVENT_MINUTES}M",
        "SUMMARY:" + summary,
        "TRANSP:TRANSPARENT",
    ]
    if recurrence is not None:
        event.append("RRULE:" + recurrence.rule())
    alarms = [timedelta(0)]
    if early is not None and early > timedelta(0):
        alarms.append(early)
    for before in alarms:
        event += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "DESCRIPTION:" + summary,
            "TRIGGER;RELATED=START:" + ("-" + _duration(before) if before else "PT0S"),
            "END:VALARM",
        ]
    event.append("END:VEVENT")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Atlas//Reminder//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        *vtimezone(zone_name, local),
        *event,
        "END:VCALENDAR",
    ]
    return ("\r\n".join(fold(line) for line in lines) + "\r\n").encode("utf-8")
