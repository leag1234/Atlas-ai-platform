"""Fictional deterministic calendar examples; no provider calls."""

import unittest
from typing import Any
from datetime import datetime
from packages.task_schedule import TaskSchedule, next_run


class ScheduleTests(unittest.TestCase):
    def schedule(self, **changes: object) -> TaskSchedule:
        return TaskSchedule.model_validate(
            dict(
                frequency="daily",
                time="02:30",
                timezone="Europe/Paris",
                anchor="2026-01-01",
                **changes,
            )
        )

    def test_spring_gap(self) -> None:
        value = self.schedule()
        self.assertEqual(
            next_run(
                value, datetime.fromisoformat("2026-03-29T00:00:00+00:00")
            ).isoformat(),
            "2026-03-29T01:30:00+00:00",
        )

    def test_autumn_runs_only_once(self) -> None:
        value = self.schedule()
        first = next_run(value, datetime.fromisoformat("2026-10-25T00:00:00+00:00"))
        self.assertEqual(first.isoformat(), "2026-10-25T00:30:00+00:00")
        self.assertEqual(
            next_run(value, first).isoformat(), "2026-10-26T01:30:00+00:00"
        )

    def test_every_three_calendar_days_and_catchup(self) -> None:
        value = TaskSchedule(
            frequency="every",
            days=3,
            time="09:00",
            timezone="Europe/Paris",
            anchor=datetime(2026, 3, 27).date(),
        )
        self.assertEqual(
            next_run(
                value, datetime.fromisoformat("2026-03-28T00:00:00+00:00")
            ).isoformat(),
            "2026-03-30T07:00:00+00:00",
        )
        self.assertEqual(
            next_run(
                value, datetime.fromisoformat("2026-04-14T12:00:00+00:00")
            ).isoformat(),
            "2026-04-17T07:00:00+00:00",
        )

    def test_weekly_and_zone(self) -> None:
        value = TaskSchedule(
            frequency="weekly",
            weekday=0,
            time="09:00",
            timezone="America/New_York",
            anchor=datetime(2026, 1, 1).date(),
        )
        self.assertEqual(
            next_run(
                value, datetime.fromisoformat("2026-10-29T00:00:00+00:00")
            ).isoformat(),
            "2026-11-02T14:00:00+00:00",
        )

    def test_invalid_zone_time_and_interval(self) -> None:
        invalid: list[dict[str, Any]] = [
            dict(timezone="Missing/Zone"),
            dict(time="24:00"),
            dict(days=0),
        ]
        for changes in invalid:
            with self.assertRaises(ValueError):
                TaskSchedule.model_validate(
                    dict(frequency="daily", time="09:00", timezone="UTC") | changes
                )
