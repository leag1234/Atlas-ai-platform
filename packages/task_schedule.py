"""REQ-PROJ scheduled tasks: calendar arithmetic in the task owner's IANA zone."""

from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frequency: Literal["daily", "every", "weekly"]
    time: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    timezone: str = Field(min_length=1, max_length=80)
    days: int = Field(default=1, ge=1, le=365, strict=True)
    weekday: int = Field(default=0, ge=0, le=6, strict=True)
    anchor: date | None = None

    @field_validator("timezone")
    @classmethod
    def zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Unknown IANA time zone") from exc
        return value


def next_run(schedule: TaskSchedule, after: datetime) -> datetime:
    """Strictly after the supplied instant; skip missed slots in one calculation.

    A nonexistent spring time moves forward by the DST gap. A repeated autumn
    time uses its first occurrence only, so a calendar day never runs twice.
    Every-N-days is anchored to a local date, never to a 24-hour UTC duration.
    """
    if after.tzinfo is None or schedule.anchor is None:
        raise ValueError("An aware clock and anchored schedule are required")
    zone = ZoneInfo(schedule.timezone)
    day = after.astimezone(zone).date()
    for offset in range(367):
        candidate_day = day + timedelta(days=offset)
        if candidate_day < schedule.anchor:
            continue
        if (
            schedule.frequency == "weekly"
            and candidate_day.weekday() != schedule.weekday
        ):
            continue
        if (
            schedule.frequency == "every"
            and (candidate_day - schedule.anchor).days % schedule.days
        ):
            continue
        local = datetime.combine(candidate_day, time.fromisoformat(schedule.time), zone)
        candidate = local.astimezone(timezone.utc)
        if candidate > after:
            return candidate
    raise ValueError("No next occurrence within schedule range")
