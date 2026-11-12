"""Bounded local spreadsheet formulas with independently computed OOXML caches.

The evaluator covers the arithmetic a person expects from a generated workbook
("pace calculated automatically" is a division): numbers, text, logical
values, cell and range references on the same workbook, the operators
+ - * / ^ & % = <> < > <= >=, parentheses, and a closed list of functions.
Anything else is rejected with a named reason, so the caller can rewrite the
formula instead of guessing. Formulas are parsed first, then evaluated, so IF
and IFERROR only evaluate the branch they return (Excel semantics).

An empty cell is read as Excel reads it: 0 in arithmetic, "" next to text and
with &, FALSE as a condition; SUM, MIN, MAX and COUNT skip it (their result
over no number is 0), and AVERAGE over no number is a division by zero, which
IFERROR catches. Text in a cell that arithmetic needs stays an error.

Text that begins like a number ("1,250" whose notation is unknown, "2 kg",
"12.50" in a header) is never skipped or misread in silence: a sum, a count, a
comparison with a number or arithmetic over it stops the render with
formula_requires_numeric_cells and the cell, and IFERROR does not hide it.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from io import BytesIO
import math
from pathlib import Path
import re
from typing import Any
from zipfile import ZipFile, ZIP_DEFLATED
from xml.etree import ElementTree as ET

from openpyxl import load_workbook
from openpyxl.formula import Tokenizer
from openpyxl.utils.cell import range_boundaries

from .cells import numeric_looking

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

FUNCTIONS = (
    "SUM",
    "SUMIF",
    "AVERAGE",
    "MIN",
    "MAX",
    "COUNT",
    "ROUND",
    "ABS",
    "IF",
    "AND",
    "OR",
    "NOT",
    "IFERROR",
)
# Excel precedence, lowest first. Prefix minus binds tighter than ^ (-2^2 = 4).
BINARY = (
    ("=", "<>", "<", ">", "<=", ">="),
    ("&",),
    ("+", "-"),
    ("*", "/"),
    ("^",),
)


class CellError(ValueError):
    """An evaluation error Excel shows in the cell (#DIV/0!, #VALUE!).

    IFERROR catches these and only these; unsupported syntax is never caught.
    """


class NumericText(str):
    """Cell text that begins like a number, with the cell that holds it."""

    where = ""


def refuse(*values: Any) -> None:
    for value in values:
        if isinstance(value, NumericText):
            raise ValueError("formula_requires_numeric_cells:" + value.where)


def cache_formulas(
    path: Path, extents: dict[str, tuple[int, int]] | None = None
) -> None:
    """Compute and store every formula result.

    extents gives, per sheet title, the written area as (rows, columns), empty
    cells included: an empty cell is not stored in the file, so the file alone
    understates the area a formula may reference.
    """
    book = load_workbook(path, data_only=False)
    values: dict[tuple[str, str], Any] = {}
    active: set[tuple[str, str]] = set()

    def number(value: Any) -> float:
        if value is None:
            return 0.0
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            refuse(value)
            raise CellError("formula_requires_numeric_cells")
        return float(value)

    def arithmetic(value: Any) -> float:
        # Logical results take part in arithmetic as 1 and 0, as in Excel.
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        return number(value)

    def logical(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, (int, float)):
            return value != 0
        raise CellError("formula_requires_logical_value")

    def text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, float):
            return format(value, ".15g")
        return str(value)

    def compare(operator: str, left: Any, right: Any) -> bool:
        # Excel orders numbers before text before logical values; an empty
        # cell is 0 next to a number, "" next to text and FALSE next to a
        # logical value.
        def blank(other: Any) -> Any:
            if isinstance(other, bool):
                return False
            return "" if isinstance(other, str) else 0.0

        if left is None:
            left = blank(right)
        if right is None:
            right = blank(left)
        if not isinstance(left, str) or not isinstance(right, str):
            # "1,250" > 1000 is not a comparison of numbers.
            refuse(left, right)

        def rank(value: Any) -> int:
            return 2 if isinstance(value, bool) else 1 if isinstance(value, str) else 0

        if rank(left) != rank(right):
            a: Any = rank(left)
            b: Any = rank(right)
        elif isinstance(left, str):
            a, b = left.casefold(), right.casefold()
        else:
            a, b = left, right
        return bool(
            {
                "=": a == b,
                "<>": a != b,
                "<": a < b,
                ">": a > b,
                "<=": a <= b,
                ">=": a >= b,
            }[operator]
        )

    def cell_value(sheet: Any, coordinate: str) -> Any:
        cell = sheet[coordinate]
        if cell.data_type != "f":
            return marked(sheet, coordinate, cell.value)
        key = (sheet.title, coordinate)
        if key in active or len(active) >= 50:
            raise ValueError("circular_or_deep_formula")
        if key not in values:
            active.add(key)
            values[key] = evaluate(sheet, cell.value)
            active.remove(key)
        return marked(sheet, coordinate, values[key])

    def marked(sheet: Any, coordinate: str, value: Any) -> Any:
        if isinstance(value, str) and numeric_looking(value):
            value = NumericText(value)
            value.where = f"{coordinate}:sheet_{sheet.title}"
        return value

    def reference(sheet: Any, text: str) -> list[Any]:
        if "!" in text:
            title, text = text.rsplit("!", 1)
            title = title.strip("'").replace("''", "'")
            if title not in book.sheetnames:
                raise ValueError("unknown_formula_sheet")
            sheet = book[title]
        if not re.fullmatch(
            r"\$?[A-Z]{1,3}\$?[1-9][0-9]{0,4}(?::\$?[A-Z]{1,3}\$?[1-9][0-9]{0,4})?",
            text,
        ):
            raise ValueError("unsupported_formula_reference")
        left, top, right, bottom = range_boundaries(text)
        if left is None or top is None or right is None or bottom is None:
            raise ValueError("unsupported_formula_reference")
        rows, columns = (extents or {}).get(sheet.title, (0, 0))
        if (
            right > max(columns, sheet.max_column)
            or bottom > max(rows, sheet.max_row)
            or (right - left + 1) * (bottom - top + 1) > 10000
        ):
            raise ValueError("formula_range_limit")
        return [
            cell_value(sheet, cell.coordinate)
            for row in sheet.iter_rows(
                min_row=top, max_row=bottom, min_col=left, max_col=right
            )
            for cell in row
        ]

    def parse(formula: str) -> tuple[Any, ...]:
        """Tokens to a small tree; nothing is evaluated here."""
        tokens = [t for t in Tokenizer(formula).items if t.type != "WHITE-SPACE"]
        if len(tokens) > 200:
            raise ValueError("formula_size_limit")
        position = 0

        def peek() -> Any:
            return tokens[position] if position < len(tokens) else None

        def binary(level: int) -> tuple[Any, ...]:
            nonlocal position
            if level == len(BINARY):
                return prefix()
            node = binary(level + 1)
            while True:
                token = peek()
                if (
                    token is None
                    or token.type != "OPERATOR-INFIX"
                    or token.value not in BINARY[level]
                ):
                    return node
                position += 1
                node = ("op", token.value, node, binary(level + 1))

        def prefix() -> tuple[Any, ...]:
            nonlocal position
            token = peek()
            if token is not None and token.type == "OPERATOR-PREFIX":
                position += 1
                if token.value not in "+-":
                    raise ValueError("unsupported_formula")
                return ("neg", prefix()) if token.value == "-" else prefix()
            return postfix()

        def postfix() -> tuple[Any, ...]:
            nonlocal position
            node = primary()
            while (token := peek()) is not None and token.type == "OPERATOR-POSTFIX":
                if token.value != "%":
                    raise ValueError("unsupported_formula")
                position += 1
                node = ("percent", node)
            return node

        def primary() -> tuple[Any, ...]:
            nonlocal position
            token = peek()
            if token is None:
                raise ValueError("incomplete_formula")
            position += 1
            if token.type == "OPERAND":
                if token.subtype == "NUMBER":
                    return ("value", float(token.value))
                if token.subtype == "TEXT":
                    return ("value", token.value[1:-1].replace('""', '"'))
                if token.subtype == "LOGICAL":
                    return ("value", token.value.upper() == "TRUE")
                if token.subtype == "RANGE":
                    return ("range", token.value)
                raise ValueError("unsupported_formula")
            if token.type == "PAREN" and token.subtype == "OPEN":
                node = binary(0)
                closing = peek()
                if (
                    closing is None
                    or closing.type != "PAREN"
                    or closing.subtype != "CLOSE"
                ):
                    raise ValueError("unsupported_formula")
                position += 1
                return node
            if token.type == "FUNC" and token.subtype == "OPEN":
                name = token.value[:-1].upper()
                if name not in FUNCTIONS:
                    raise ValueError("unsupported_formula_function")
                args: list[tuple[Any, ...]] = []
                closing = peek()
                if not (
                    closing is not None
                    and closing.type == "FUNC"
                    and closing.subtype == "CLOSE"
                ):
                    args.append(binary(0))
                    while (separator := peek()) is not None and (
                        separator.type == "SEP" and separator.subtype == "ARG"
                    ):
                        position += 1
                        args.append(binary(0))
                closing = peek()
                if (
                    closing is None
                    or closing.type != "FUNC"
                    or closing.subtype != "CLOSE"
                ):
                    raise ValueError("unsupported_formula")
                position += 1
                return ("call", name, tuple(args))
            raise ValueError("unsupported_formula")

        tree = binary(0)
        if position != len(tokens):
            raise ValueError("unsupported_formula")
        return tree

    def values_of(sheet: Any, node: tuple[Any, ...]) -> list[Any]:
        """A range argument yields its cells; any other argument one value."""
        if node[0] == "range":
            return reference(sheet, node[1])
        return [scalar(sheet, node)]

    def scalar(sheet: Any, node: tuple[Any, ...]) -> Any:
        kind = node[0]
        if kind == "value":
            return node[1]
        if kind == "range":
            cells = reference(sheet, node[1])
            if len(cells) != 1:
                raise ValueError("formula_range_needs_single_cell")
            return cells[0]
        if kind == "neg":
            return -arithmetic(scalar(sheet, node[1]))
        if kind == "percent":
            return arithmetic(scalar(sheet, node[1])) / 100
        if kind == "op":
            operator, left, right = (
                node[1],
                scalar(sheet, node[2]),
                scalar(sheet, node[3]),
            )
            if operator == "&":
                return text(left) + text(right)
            if operator in BINARY[0]:
                return compare(operator, left, right)
            a, b = arithmetic(left), arithmetic(right)
            if operator == "+":
                return a + b
            if operator == "-":
                return a - b
            if operator == "*":
                return a * b
            if operator == "/":
                if b == 0:
                    raise CellError("formula_division_by_zero")
                return a / b
            try:
                result = a**b
            except (OverflowError, ZeroDivisionError):
                raise CellError("nonfinite_formula") from None
            if isinstance(result, complex):
                raise CellError("nonfinite_formula")
            return result
        return function(sheet, node[1], node[2])

    def function(sheet: Any, name: str, args: tuple[tuple[Any, ...], ...]) -> Any:
        def arity(low: int, high: int) -> None:
            if not low <= len(args) <= high:
                raise ValueError("formula_argument_count")

        def numbers() -> list[float]:
            # Ranges skip text, logical values and empty cells, like Excel;
            # text that begins like a number is refused, never skipped.
            found: list[float] = []
            for arg in args:
                for value in values_of(sheet, arg):
                    refuse(value)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        found.append(float(value))
            return found

        if name == "IF":
            arity(2, 3)
            if logical(scalar(sheet, args[0])):
                return scalar(sheet, args[1])
            return scalar(sheet, args[2]) if len(args) == 3 else False
        if name == "IFERROR":
            arity(2, 2)
            try:
                return scalar(sheet, args[0])
            except CellError:
                return scalar(sheet, args[1])
        if name in {"AND", "OR"}:
            arity(1, 30)
            flags = [
                logical(value)
                for arg in args
                for value in values_of(sheet, arg)
                if value is not None and not isinstance(value, str)
            ]
            if not flags:
                raise CellError("formula_requires_logical_value")
            return all(flags) if name == "AND" else any(flags)
        if name == "NOT":
            arity(1, 1)
            return not logical(scalar(sheet, args[0]))
        if name == "ABS":
            arity(1, 1)
            return abs(arithmetic(scalar(sheet, args[0])))
        if name == "ROUND":
            arity(2, 2)
            value = arithmetic(scalar(sheet, args[0]))
            digits = arithmetic(scalar(sheet, args[1]))
            if not math.isfinite(value) or not -15 <= digits <= 15:
                raise CellError("nonfinite_formula")
            # Half away from zero, as in Excel (Python's round is half to even).
            step = Decimal(1).scaleb(-int(digits))
            return float(Decimal(repr(value)).quantize(step, rounding=ROUND_HALF_UP))
        if name == "SUMIF":
            if len(args) not in (2, 3):
                raise ValueError("formula_argument_count")
            criteria = values_of(sheet, args[0])
            criterion_values = values_of(sheet, args[1])
            if len(criterion_values) != 1:
                raise ValueError("unsupported_sumif_criteria")
            criterion = criterion_values[0]
            amounts = values_of(sheet, args[2]) if len(args) == 3 else criteria
            if len(criteria) != len(amounts) or (
                isinstance(criterion, str) and any(c in criterion for c in "*?~<>=!")
            ):
                raise ValueError("unsupported_sumif_criteria")

            def meets(value: Any) -> bool:
                # An empty criteria cell matches no criterion. A number cell is
                # compared as a number: 12.5 typed "12.50" meets "12.50".
                if value is None:
                    return False
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and not isinstance(criterion, bool)
                ):
                    try:
                        return float(criterion) == value
                    except (TypeError, ValueError):
                        return False
                return str(value).casefold() == str(criterion).casefold()

            return sum(
                number(v) for c, v in zip(criteria, amounts, strict=True) if meets(c)
            )
        arity(1, 30)
        found = numbers()
        if name == "SUM":
            return sum(found)
        if name == "COUNT":
            return float(len(found))
        if name == "AVERAGE":
            if not found:
                raise CellError("formula_division_by_zero")
            return sum(found) / len(found)
        if name == "MIN":
            return min(found, default=0.0)
        return max(found, default=0.0)

    def evaluate(sheet: Any, formula: str) -> Any:
        result = scalar(sheet, parse(formula))
        if result is None:
            # "=B2" on an empty cell shows 0.
            return 0.0
        if isinstance(result, bool):
            return result
        if isinstance(result, str):
            return str(result)
        value = float(result)
        if not math.isfinite(value):
            raise ValueError("nonfinite_formula")
        # The negation of an empty cell is 0, never a stored "-0.0".
        return value + 0.0

    try:
        for sheet in book:
            for row in sheet:
                for cell in row:
                    if cell.data_type == "f":
                        cell_value(sheet, cell.coordinate)
        output = BytesIO()
        with ZipFile(path) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
            for name in source.namelist():
                raw = source.read(name)
                match = re.fullmatch(r"xl/worksheets/sheet([0-9]+)\.xml", name)
                if match:
                    sheet = book.worksheets[int(match[1]) - 1]
                    root = ET.fromstring(raw)
                    for xml_cell in root.iter(NS + "c"):
                        key = (sheet.title, xml_cell.attrib["r"])
                        if key in values:
                            xml_value = xml_cell.find(NS + "v")
                            if xml_value is None:
                                xml_value = ET.SubElement(xml_cell, NS + "v")
                            cached = values[key]
                            if isinstance(cached, bool):
                                xml_cell.set("t", "b")
                                xml_value.text = "1" if cached else "0"
                            elif isinstance(cached, str):
                                xml_cell.set("t", "str")
                                xml_value.text = cached
                            else:
                                xml_cell.attrib.pop("t", None)
                                xml_value.text = repr(cached)
                    raw = ET.tostring(root, encoding="utf-8", xml_declaration=True)
                target.writestr(name, raw)
        path.write_bytes(output.getvalue())
        reopened = load_workbook(path, data_only=True)
        try:
            for (sheet_name, coordinate), value in values.items():
                found = reopened[sheet_name][coordinate].value
                if isinstance(value, str) and found is None:
                    found = ""
                if found != value or type(found) is not type(value):
                    if not (
                        isinstance(value, float)
                        and isinstance(found, (int, float))
                        and not isinstance(found, bool)
                        and found == value
                    ):
                        raise ValueError("formula_cache_verification_failed")
        finally:
            reopened.close()
    finally:
        book.close()
