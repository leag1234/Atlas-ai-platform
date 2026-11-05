import calendar
import random
import unittest
from datetime import datetime, timezone

from metrics.daily import daily_counts, day_of, day_start, window


class HiddenUTC(unittest.TestCase):
    def test_random_days(self):
        rng = random.Random(4)
        for _ in range(500):
            ts = rng.randint(0, 2_000_000_000)
            self.assertEqual(day_of(ts), datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"))
            d = day_of(ts)
            self.assertEqual(day_start(d), calendar.timegm(datetime.strptime(d, "%Y-%m-%d").timetuple()))

    def test_dst_dates(self):
        for d in ("2026-03-29", "2026-10-25", "2026-04-05", "2026-09-27"):
            self.assertEqual(day_of(day_start(d)), d)
            self.assertEqual(day_start(d) % 86400, 0)

    def test_window_and_counts(self):
        s = day_start("2026-06-10")
        stamps = [s - 1, s, s + 3600 * 23, s + 86400 * 2 - 1, s + 86400 * 2]
        self.assertEqual(window(stamps, "2026-06-10", "2026-06-11"), [s, s + 3600 * 23, s + 86400 * 2 - 1])
        self.assertEqual(daily_counts(stamps), {"2026-06-09": 1, "2026-06-10": 2, "2026-06-11": 1, "2026-06-12": 1})
