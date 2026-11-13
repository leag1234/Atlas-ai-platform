"""REQ-TOOL-002: exact arithmetic, no executable expressions."""

import json
import unittest
from decimal import Decimal

from packages.limits import LimitError
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

    def test_powers(self) -> None:
        for expr, expected in [
            ("2**10", "1024"),
            ("2^10", "1024"),
            # ^ is a power with the usual precedence, never a bitwise operator.
            ("2^3+1", "9"),
            ("-2^2", "-4"),
            ("(-2)^3", "-8"),
            ("2^-2", "0.25"),
            ("16^0.25", "2"),
            ("pow(1.5, 2)", "2.25"),
            ("0^0", "1"),
            ("0^3", "0"),
            ("round(15000*(1+0.032)^12, 2)", "21890.09"),
            ("round(8000*(1+0.045/12)^60, 2)", "10014.37"),
        ]:
            with self.subTest(expr=expr):
                self.assertEqual(Decimal(calculate(expr)), Decimal(expected))

    def test_square_root(self) -> None:
        self.assertEqual(Decimal(calculate("sqrt(144)")), 12)
        self.assertEqual(Decimal(calculate("SQRT(81) + 1")), 10)
        self.assertTrue(calculate("sqrt(2)").startswith("1.41421356237309504880"))
        self.assertEqual(Decimal(calculate("round(sqrt(3^2 + 4^2))")), 5)

    def test_rounding_is_half_up_and_keeps_decimals(self) -> None:
        for expr, expected in [
            ("round(2.345, 2)", "2.35"),
            ("round(2.5)", "3"),
            ("round(-2.5)", "-3"),
            ("round(2.5, 2)", "2.50"),
            ("round(1/3, 4)", "0.3333"),
            ("round(1234.5, -2)", "1200"),
        ]:
            with self.subTest(expr=expr):
                self.assertEqual(calculate(expr), expected)

    def test_percentages(self) -> None:
        for expr, expected in [
            ("percent(20, 149.90)", "29.98"),
            ("percent(5.5, 24.40) + 24.40", "25.742"),
            ("round(percent_change(780, 845), 2)", "8.33"),
            ("round(percent_change(48.20, 41.65), 2)", "-13.59"),
            ("percent_change(-50, -25)", "50"),
        ]:
            with self.subTest(expr=expr):
                self.assertEqual(Decimal(calculate(expr)), Decimal(expected))

    def test_bounds(self) -> None:
        for expr, unit, measured in [
            ("2^10001", "exponent", 10001),
            ("pow(2, -20000)", "exponent", 20000),
            ("9^9^9", "exponent", 387420489),
            ("10^51", "result digits", 51),
            ("0.1^60", "result digits", 60),
            ("10^50 * 10", "absolute value", 10**51),
        ]:
            with self.subTest(expr=expr), self.assertRaises(LimitError) as caught:
                calculate(expr)
            self.assertEqual(caught.exception.unit, unit)
            self.assertEqual(caught.exception.measured, measured)

    def test_clear_errors(self) -> None:
        for expr, message in [
            ("sqrt(-1)", "square root of a negative number"),
            ("(-8)^(1/3)", "fractional power of a negative number"),
            ("0^-1", "division by zero"),
            ("round(1.25, 1.5)", "round needs a whole number of decimals"),
            ("round(1.25, 21)", "round needs a whole number of decimals"),
            ("percent_change(0, 5)", "percent_change from zero"),
            ("sqrt(2, 5)", "sqrt takes 1 argument(s)"),
            ("round(1, 2, 3)", "round takes 1 or 2 argument(s)"),
            ("1,5+2", "use a dot for decimals"),
            ("15%*200", "write percent(p, x), not %"),
            ("exp(1)", "unknown name exp"),
        ]:
            with self.subTest(expr=expr), self.assertRaises(ValueError) as caught:
                calculate(expr)
            self.assertIn(message, str(caught.exception))
            # The tool layer only passes errors shorter than 80 characters.
            self.assertLess(len(str(caught.exception)), 80)

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
            "sqrt",
            "sqrt(x=4)",
            "(1).real",
            "sqrt.__class__",
            "pow(*[2, 3])",
            "lambda: 1",
            "1 if 1 else 2",
            "sqrt(" * 100 + "4" + ")" * 100,
        ]:
            with self.subTest(expr=expr), self.assertRaises(ValueError):
                calculate(expr)

    def test_description_lists_operations_and_schema_is_unchanged(self) -> None:
        # The grammar is documented in code; the tool schema sent to the model stays
        # as recorded in the journey replays until the next recording refresh.
        from services.orchestrator.calculator import DESCRIPTION
        from services.orchestrator.tools import declarations

        for operation in ("sqrt(x)", "pow(x, y)", "round(x, n)", "percent(p, x)"):
            self.assertIn(operation, DESCRIPTION)
        self.assertIn("percent_change(old, new)", DESCRIPTION)
        tools = json.loads(json.dumps(declarations()))
        calculator = next(
            tool["function"]
            for tool in tools
            if tool["function"]["name"] == "calculator"
        )
        self.assertNotIn("description", calculator["parameters"]["properties"]["expr"])
