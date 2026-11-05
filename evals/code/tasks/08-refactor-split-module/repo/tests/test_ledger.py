import unittest
from decimal import Decimal

from ledger import LedgerError, balances, format_report, parse_amount, parse_file, parse_line


class LedgerTest(unittest.TestCase):
    def test_amount(self):
        self.assertEqual(parse_amount("2,400.5"), Decimal("2400.50"))
        with self.assertRaises(LedgerError):
            parse_amount("-1")

    def test_balances(self):
        entries = parse_file("sample.ledger")
        b = balances(entries)
        self.assertEqual(b["assets:bank"].balance, Decimal("400.00"))
        self.assertEqual(b["assets:cash"].balance, Decimal("57.65"))

    def test_line(self):
        e = parse_line("2026-03-01 | a:x | b:y | 1 ")
        self.assertEqual(e.memo, "")

    def test_report(self):
        text = format_report(parse_file("sample.ledger"))
        self.assertIn("expenses 2026-02", text)


if __name__ == "__main__":
    unittest.main()
