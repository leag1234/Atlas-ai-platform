import unittest

from metrics.daily import daily_counts, day_of, day_start, window

# 2026-03-01T00:00:00Z
MARCH_1 = 1772323200


class DailyTest(unittest.TestCase):
    def test_day_of(self):
        self.assertEqual(day_of(MARCH_1), "2026-03-01")
        self.assertEqual(day_of(MARCH_1 - 1), "2026-02-28")

    def test_day_start(self):
        self.assertEqual(day_start("2026-03-01"), MARCH_1)

    def test_counts(self):
        stamps = [MARCH_1 + 10, MARCH_1 + 86399, MARCH_1 + 86400, MARCH_1 - 5]
        self.assertEqual(daily_counts(stamps), {"2026-02-28": 1, "2026-03-01": 2, "2026-03-02": 1})

    def test_window(self):
        stamps = [MARCH_1 - 1, MARCH_1, MARCH_1 + 86399, MARCH_1 + 86400]
        self.assertEqual(window(stamps, "2026-03-01", "2026-03-01"), [MARCH_1, MARCH_1 + 86399])


if __name__ == "__main__":
    unittest.main()
