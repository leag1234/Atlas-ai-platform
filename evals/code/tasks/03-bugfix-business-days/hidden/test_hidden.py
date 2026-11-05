import random
import unittest
from datetime import date, timedelta

from payroll import add_business_days, count_business_days


def brute(start, end, holidays):
    hs = set(holidays)
    n, d = 0, start
    while d < end:
        if d.weekday() < 5 and d not in hs:
            n += 1
        d += timedelta(days=1)
    return n


class HiddenCalendar(unittest.TestCase):
    def test_reported(self):
        self.assertEqual(count_business_days(date(2026, 11, 6), date(2026, 11, 10)), 2)

    def test_weekend_holiday(self):
        sat = date(2026, 11, 7)
        self.assertEqual(count_business_days(date(2026, 11, 2), date(2026, 11, 16), [sat]), 10)

    def test_duplicate_holiday(self):
        h = date(2026, 11, 11)
        self.assertEqual(count_business_days(date(2026, 11, 9), date(2026, 11, 16), [h, h]), 4)

    def test_empty_and_invalid(self):
        self.assertEqual(count_business_days(date(2026, 1, 1), date(2026, 1, 1)), 0)
        with self.assertRaises(ValueError):
            count_business_days(date(2026, 1, 2), date(2026, 1, 1))

    def test_random(self):
        rng = random.Random(11)
        base = date(2026, 1, 1)
        for _ in range(800):
            s = base + timedelta(days=rng.randint(0, 400))
            e = s + timedelta(days=rng.randint(0, 40))
            hol = [base + timedelta(days=rng.randint(0, 450)) for _ in range(rng.randint(0, 30))]
            self.assertEqual(count_business_days(s, e, hol), brute(s, e, hol), (s, e))
            self.assertEqual(count_business_days(s, e, iter(hol)), brute(s, e, hol))

    def test_add_consistent(self):
        rng = random.Random(5)
        base = date(2026, 3, 1)
        for _ in range(200):
            s = base + timedelta(days=rng.randint(0, 100))
            n = rng.randint(0, 25)
            hol = [base + timedelta(days=rng.randint(0, 150)) for _ in range(5)]
            d = add_business_days(s, n, hol)
            first = add_business_days(s, 0, hol)
            self.assertEqual(count_business_days(first, d, hol), n)
