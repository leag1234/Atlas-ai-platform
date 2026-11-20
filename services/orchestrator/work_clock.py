"""The date and time a Work turn starts with, in the person's own time zone (T8).

A Work turn writes notes, reports and files on its own. Without the date it
guessed one: a scheduled daily note run on 2026-11-16 was dated 2025-11-19
(staging). The chat computes dates with the date_time tool on the turns about
them (exact_tools); every Work turn instead receives the current moment read by
code as trusted session context, in the zone of the person who asked (the task
creator for a scheduled run). Nothing here goes into prompts/.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.orchestrator import datecalc

WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def person_zone(name: str | None, *, explicit: bool = False) -> str:
    """The IANA zone of the person this turn works for.

    A browser sends its own zone; "UTC" is the request default when none was
    sent, so it falls back to the chat's home zone (datecalc.DEFAULT_ZONE) like
    the date tool does. A zone stored with a scheduled task was chosen by its
    creator: it is used as given, UTC included.
    """
    if name and (explicit or name != "UTC"):
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
        else:
            return name
    return datecalc.DEFAULT_ZONE


def located_zone(location: Any) -> str | None:
    """The zone of a location the browser sent: its own zone, sent with it,
    else the zone at the device's position; None when neither is known."""
    if location is None:
        return None
    zone = getattr(location, "timezone", None)
    if zone and zone != "UTC":
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            pass
        else:
            return str(zone)
    position = getattr(location, "position", None)
    if position is None:
        return None
    from services.orchestrator.transit_planner import zone_at

    return zone_at(position.latitude, position.longitude) or None


def settle_zone(request: Any) -> str:
    """Make ``request.ui_timezone`` the person's zone before anything reads it.

    Every "today" of a chat turn (the calendar note, the date checks, the
    trip's days, the date tool, reminders) reads ``ui_timezone``. Open WebUI
    forwards no zone of its own, so it stayed "UTC" and they all fell back to
    the instance's default zone (staging -133: in New York at 19:25 on the
    19th, the model was told tomorrow was the 21st, while the journey planner,
    in the device's zone, rightly planned the 20th). When the interface sent
    none, the zone of the location it sent (or the one shared earlier in this
    conversation) is the person's; else the instance default stays.
    """
    zone = getattr(request, "ui_timezone", None)
    if zone and zone != "UTC":
        return str(zone)
    location = getattr(request, "location", None)
    if location is None:
        from services.orchestrator.location_memory import conversation_key, positions

        try:
            location = positions.recall(conversation_key(request))
        except (AttributeError, TypeError):
            location = None
    found = located_zone(location)
    if found:
        request.ui_timezone = found
        return found
    return zone or "UTC"


def request_zone(request: Any) -> str:
    scheduled = bool(getattr(request, "_scheduled_run", False))
    zone = getattr(request, "ui_timezone", None)
    if not scheduled and (not zone or zone == "UTC"):
        zone = located_zone(getattr(request, "location", None)) or zone
    return person_zone(zone, explicit=scheduled)


def current_moment(zone: str, now: datetime | None = None) -> dict[str, str]:
    """Today's date, weekday and time in ``zone``, read from the clock by code."""
    instant = (now or datecalc.utc_now()).astimezone(ZoneInfo(zone))
    offset = instant.strftime("%z")
    return dict(
        date=instant.date().isoformat(),
        weekday=WEEKDAYS[instant.weekday()],
        time=instant.strftime("%H:%M"),
        time_zone=zone,
        utc_offset=offset[:3] + ":" + offset[3:],
    )


def moment_line(moment: dict[str, str]) -> str:
    return (
        f"Current date and time: {moment['weekday']} {moment['date']} {moment['time']} "
        f"({moment['time_zone']}, UTC{moment['utc_offset']}). Use this date for today; "
        "never guess another."
    )


def scheduled_deliverable(target: str | None, exists: bool | None) -> str | None:
    """The deliverable note of a scheduled run, for the model only.

    It was appended to the task's instruction and so shown to the user as their
    own message in the run's conversation (staging 2026-11-16). It also always
    said the previous version was kept, even for a file that did not exist yet.
    """
    if not target:
        return None
    office = target.lower().endswith((".docx", ".pptx", ".xlsx", ".pdf"))
    tool = "publish_document" if office else "publish_scheduled_file"
    state = (
        "It already exists: publishing adds a new version and keeps the earlier ones."
        if exists
        else "It does not exist yet: publishing creates it, so there is no previous version to mention."
    )
    return f"Scheduled deliverable: publish the result to {target} with {tool}. {state}"


def work_context(request: Any, now: datetime | None = None) -> dict[str, Any]:
    """Trusted session context every Work turn starts with."""
    context: dict[str, Any] = {"now": current_moment(request_zone(request), now)}
    note = scheduled_deliverable(
        getattr(request, "_scheduled_target", None),
        request.__dict__.get("scheduled_target_exists"),
    )
    if note is not None:
        context["scheduled_deliverable"] = note
    return context


def engine_preamble(request: Any, now: datetime | None = None) -> str:
    """The same context as text, for the coding engine's first message."""
    context = work_context(request, now)
    lines = [moment_line(context["now"])]
    if "scheduled_deliverable" in context:
        lines.append(context["scheduled_deliverable"])
    return "\n".join(lines)
