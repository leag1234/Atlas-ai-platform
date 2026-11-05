import unittest
from datetime import date

from payroll import add_business_days, count_business_days


class CalendarTest(unittest.TestCase):
    def test_monday_week(self):
        self.assertEqual(count_business_days(date(2026, 11, 2), date(2026, 11, 9)), 5)

    def test_add(self):
        self.assertEqual(add_business_days(date(2026, 11, 6), 1), date(2026, 11, 9))


if __name__ == "__main__":
    unittest.main()
