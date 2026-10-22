"""REQ-TOOL-002: exact arithmetic, no executable expressions."""

import unittest
from decimal import Decimal

from services.orchestrator.calculator import calculate


class CalculatorTests(unittest.TestCase):
    def test_decimal(self) -> None:
        for expr, expected in [
            ("1847*293*(1-0.12)", "476230.48"),
            ("3480.50*0.21", "730.905"),
            ("0.1+0.2", "0.3"),
            ("-(4+2)/3", "-2"),
            ("+2-1", "1"),
        ]:
            with self.subTest(expr=expr):
                self.assertEqual(Decimal(calculate(expr)), Decimal(expected))

    def test_reject(self) -> None:
        for expr in [
            "",
            " ",
            "2**999999",
            "1/0",
            "True",
            "x",
            "1e999999",
            '__import__("os")',
            "[1]",
            "2%1",
            "1+" * 300,
            "١+2",
        ]:
            with self.subTest(expr=expr), self.assertRaises(ValueError):
                calculate(expr)
