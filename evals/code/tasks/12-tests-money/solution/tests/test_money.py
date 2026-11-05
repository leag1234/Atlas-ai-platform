import unittest

from money import apply_discount, format_amount, split


class SplitTest(unittest.TestCase):
    def test_split(self):
        self.assertEqual(split(100, 3), [34, 33, 33])
        self.assertEqual(split(-100, 3), [-34, -33, -33])
        self.assertEqual(split(5, 1), [5])
        self.assertEqual(split(2, 4), [1, 1, 0, 0])
        with self.assertRaises(ValueError):
            split(10, 0)


class DiscountTest(unittest.TestCase):
    def test_discount(self):
        self.assertEqual(apply_discount(1000, 10), 900)
        self.assertEqual(apply_discount(50, 1), 49)   # 0.5 cent rounds up
        self.assertEqual(apply_discount(150, 1), 148)  # 1.5 cents rounds to 2
        self.assertEqual(apply_discount(1000, 12.5), 875)
        self.assertEqual(apply_discount(1000, 100), 0)
        self.assertEqual(apply_discount(1000, 0), 1000)
        for bad in (-1, 100.5):
            with self.assertRaises(ValueError):
                apply_discount(1000, bad)


class FormatTest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(format_amount(123450), "1,234.50")
        self.assertEqual(format_amount(5), "0.05")
        self.assertEqual(format_amount(-5), "-0.05")
        self.assertEqual(format_amount(123450, "EUR"), "1,234.50 EUR")
        self.assertEqual(format_amount(100000000), "1,000,000.00")
