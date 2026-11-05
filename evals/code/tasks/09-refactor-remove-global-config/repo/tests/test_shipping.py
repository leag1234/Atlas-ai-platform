import unittest
from decimal import Decimal

from shipping.invoice import render
from shipping.labels import make_label
from shipping.rates import quote


class ShippingTest(unittest.TestCase):
    def test_quote(self):
        self.assertEqual(quote(2.5, "10"), Decimal("7.90"))
        self.assertEqual(quote(2.5, "60"), Decimal("0.00"))
        with self.assertRaises(ValueError):
            quote(0, "10")

    def test_label(self):
        self.assertEqual(make_label(42), "SHP-00000042")

    def test_render(self):
        self.assertEqual(render(7, 1, "5"), "SHP-00000007 shipping 6.10 EUR")


if __name__ == "__main__":
    unittest.main()
