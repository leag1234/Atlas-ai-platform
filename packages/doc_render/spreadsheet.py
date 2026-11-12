"""Bind table summaries to written data and check publication against source rows."""

from collections import Counter
from decimal import Decimal, InvalidOperation
from io import BytesIO
import math
import re
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import column_index_from_string

from .schema import Content

SUMIF = re.compile(
    r'^=SUMIF\((?:\'?[0-9]+\'?!)?\$?([A-Z]+)\$?\d+:\$?\1\$?\d+,\s*"([^"]+)",\s*(?:\'?[0-9]+\'?!)?\$?([A-Z]+)\$?\d+:\$?\3\$?\d+\)$',
    re.I,
)
SUM = re.compile(r"^=SUM\((?:'?[0-9]+'?!)?\$?([A-Z]+)\$?(\d+):\$?\1\$?(\d+)\)$", re.I)


CELL = r"\$?([A-Z]{1,3})\$?([0-9]{1,5})"
# A total keeps the unit of what it adds up: one range in SUM, AVERAGE, MIN or
# MAX, the amounts of a SUMIF, or cells added and subtracted.
TOTAL = re.compile(
    rf"=(?:(?:SUM|AVERAGE|MIN|MAX)\(|SUMIF\([^,()]+,[^,()]+,){CELL}:{CELL}\)", re.I
)
TERMS = re.compile(rf"={CELL}(?:[+-]{CELL})+", re.I)


def total_formats(
    typed: dict[str, tuple[float, str]], computed: dict[str, str]
) -> dict[str, str]:
    """The number format of each total, taken from the cells it adds up.

    typed gives the value and format of each number cell, computed the formula
    of each formula cell. Twelve months typed "1 250,00 €" give a total shown
    the same way; cells of different kinds give no format.
    """
    sources: dict[str, list[str]] = {}
    for coordinate, formula in computed.items():
        match = TOTAL.fullmatch(formula)
        if match:
            left, right = sorted(
                (column_index_from_string(match[1]), column_index_from_string(match[3]))
            )
            top, bottom = sorted((int(match[2]), int(match[4])))
            if (right - left + 1) * (bottom - top + 1) <= 10000:
                sources[coordinate] = [
                    f"{get_column_letter(column)}{row}"
                    for row in range(top, bottom + 1)
                    for column in range(left, right + 1)
                ]
        elif TERMS.fullmatch(formula):
            sources[coordinate] = [
                (column + row).upper()
                for column, row in re.findall(CELL, formula, re.I)
            ]
    found: dict[str, str] = {}
    for _ in range(3):
        # A total of totals is settled once its own cells are.
        for coordinate, cells in sources.items():
            if coordinate in found:
                continue
            if any(cell in sources and cell not in found for cell in cells):
                continue
            formats = [typed[cell][1] for cell in cells if cell in typed]
            formats += [found[cell] for cell in cells if cell in found]
            kinds = {shown.replace("#,##0", "0") for shown in formats}
            if len(kinds) != 1 or "General" in kinds:
                found[coordinate] = "General"
                continue
            # Amounts under a thousand show no preference: digits are grouped
            # unless a larger amount was typed without grouping.
            ungrouped = any(
                abs(typed[cell][0]) >= 1000 and "#,##0" not in typed[cell][1]
                for cell in cells
                if cell in typed
            ) or any("#,##0" not in found[cell] for cell in cells if cell in found)
            kind = kinds.pop()
            found[coordinate] = kind if ungrouped else kind.replace("0", "#,##0", 1)
    return {cell: shown for cell, shown in found.items() if shown != "General"}


# One SUM, AVERAGE, MIN or MAX over one range, and nothing else.
AGGREGATE = re.compile(rf"=(?:SUM|AVERAGE|MIN|MAX)\({CELL}:{CELL}\)", re.I)


def table_areas(content: Content) -> dict[str, tuple[int, int]]:
    """Per sheet title, the rows (header included) and columns of its table.

    The contract of a workbook: the first table of a section starts at cell A1
    of its sheet, header in row 1 and first data row in row 2, so a reference
    written by the author of the table is the reference in the file. Only that
    table holds formulas, and a formula refers to cells of such a table (its
    own, or the one of a numbered sheet): the position of anything else on a
    sheet depends on the texts written around it. A later table of a section
    that holds a formula is refused with its sheet.
    """
    areas: dict[str, tuple[int, int]] = {}
    for index, section in enumerate(content.sections, 1):
        for position, table in enumerate(section.tables):
            if position == 0:
                areas[str(index)] = (1 + len(table.rows), len(table.headers))
            elif table.formulas and any(
                cell.startswith("=") for row in table.rows for cell in row
            ):
                raise ValueError(f"formula_table_not_first:sheet_{index}")
    return areas


def total_findings(raw: bytes, areas: dict[str, tuple[int, int]]) -> list[str]:
    """Totals of a rendered workbook whose range is probably not the one meant.

    A total is one SUM, AVERAGE, MIN or MAX over a range of its own column,
    written under that range. Two findings, each with the cell and its sheet:
    total_includes_header when the range starts on the header row, and
    total_stops_early when a cell that is not a formula lies between the end
    of the range and the total (a row of the column that the total leaves
    out). Publication does not refuse them: a header is text, which a sum
    skips, and a total of the first rows only can be meant. They let a test
    prove what the totals of a rendered file add up.
    """
    book = load_workbook(BytesIO(raw), data_only=False)
    try:
        findings: list[str] = []
        for sheet in book:
            rows, columns = areas.get(sheet.title, (0, 0))
            if not rows:
                continue
            for row in range(2, rows + 1):
                for column in range(1, columns + 1):
                    cell = sheet.cell(row, column)
                    if cell.data_type != "f":
                        continue
                    match = AGGREGATE.fullmatch(str(cell.value))
                    if (
                        match is None
                        or match[1].upper() != match[3].upper()
                        or column_index_from_string(match[1].upper()) != column
                    ):
                        continue
                    top, bottom = sorted((int(match[2]), int(match[4])))
                    if bottom >= row:
                        continue
                    where = f"{cell.coordinate}:sheet_{sheet.title}"
                    if top <= 1:
                        findings.append("total_includes_header:" + where)
                    if any(
                        sheet.cell(between, column).data_type != "f"
                        for between in range(bottom + 1, row)
                    ):
                        findings.append("total_stops_early:" + where)
        return findings
    finally:
        book.close()


def bind_summaries(content: Content) -> Content:
    value = content.model_dump()
    for section in value["sections"]:
        # Formulas live in the first table of a section, which starts at A1:
        # its data rows start at row 2 (see table_areas).
        for table in section["tables"][:1]:
            rows = table["rows"]
            start = 2
            data = []
            for row in rows:
                if any(cell.startswith("=") for cell in row):
                    break
                data.append(row)
            end = start + len(data) - 1
            if table["formulas"] and data:
                replacements: dict[str, str] = {}
                for row in rows[len(data) :]:
                    for cell in row:
                        match = SUMIF.fullmatch(cell)
                        if (
                            match
                            and "!" not in cell
                            and max(
                                column_index_from_string(match[1]),
                                column_index_from_string(match[3]),
                            )
                            <= len(table["headers"])
                        ):
                            replacements[cell] = (
                                f'=SUMIF({match[1]}{start}:{match[1]}{end},"{match[2]}",{match[3]}{start}:{match[3]}{end})'
                            )
                # A range with exactly the data-row count is a table aggregate.
                # SUM over summary cells or explicit subsets remains unchanged.
                for row in rows[len(data) :]:
                    for cell in row:
                        match = SUM.fullmatch(cell)
                        if (
                            match
                            and "!" not in cell
                            and int(match[3]) - int(match[2]) + 1 == len(data)
                            and int(match[2]) < start + len(data)
                        ):
                            replacements[cell] = (
                                f"=SUM({match[1]}{start}:{match[1]}{end})"
                            )
                table["rows"] = tuple(
                    tuple(replacements.get(cell, cell) for cell in row) for row in rows
                )
    return Content.model_validate(value)


def scalar(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    try:
        number = Decimal(text)
        if number.is_finite():
            return str(number.normalize())
    except InvalidOperation:
        pass
    return text


def validate_sources(raw: bytes, selections: list[list[dict[str, Any]]]) -> None:
    """No source row may disappear; cached category totals use source amounts."""
    if not selections:
        return
    source_rows: list[tuple[str, ...]] = []
    for segments in selections:
        rows: dict[tuple[str, int], dict[int, str]] = {}
        for segment in segments:
            locator = segment["locator"]
            match = re.fullmatch(r"([A-Z]+)([0-9]+)", locator.get("cell", ""))
            if match:
                rows.setdefault((locator["sheet"], int(match[2])), {})[
                    column_index_from_string(match[1])
                ] = scalar(segment["text"])
        for cells in rows.values():
            source_row = tuple(
                cells.get(col, "") for col in range(min(cells), max(cells) + 1)
            )
            if any(
                re.fullmatch(r"-?\d+(?:\.\d+)?(?:E[+-]?\d+)?", cell)
                for cell in source_row
            ):
                source_rows.append(source_row)
    if not source_rows:
        raise ValueError("spreadsheet_source_mismatch:no_numeric_source_rows")
    formulas = load_workbook(BytesIO(raw), data_only=False)
    cached = load_workbook(BytesIO(raw), data_only=True)
    try:
        actual = Counter(
            tuple(scalar(cell.value) for cell in row)
            for sheet in formulas
            for row in sheet
            if not any(cell.data_type == "f" for cell in row)
        )
        required = Counter(source_rows)
        if required - actual:
            raise ValueError("spreadsheet_source_mismatch:missing_rows")
        for sheet in formulas:
            for row in sheet:
                for cell in row:
                    if cell.data_type != "f":
                        continue
                    match = SUMIF.fullmatch(str(cell.value))
                    if not match:
                        if "SUMIF(" in str(cell.value).upper():
                            raise ValueError(
                                "spreadsheet_source_mismatch:unverifiable_formula"
                            )
                        total = SUM.fullmatch(str(cell.value))
                        if total:
                            col = column_index_from_string(total[1]) - 1
                            if all(col < len(source) for source in source_rows):
                                try:
                                    expected_total = sum(
                                        Decimal(source[col]) for source in source_rows
                                    )
                                except InvalidOperation:
                                    continue
                                actual_total = cached[sheet.title][
                                    cell.coordinate
                                ].value
                                if not isinstance(
                                    actual_total, (int, float)
                                ) or not math.isclose(
                                    float(expected_total),
                                    actual_total,
                                    rel_tol=1e-12,
                                    abs_tol=1e-12,
                                ):
                                    raise ValueError(
                                        "spreadsheet_source_mismatch:cached_total"
                                    )
                        continue
                    category, amount = (
                        column_index_from_string(match[1]) - 1,
                        column_index_from_string(match[3]) - 1,
                    )
                    if any(
                        max(category, amount) >= len(source) for source in source_rows
                    ):
                        raise ValueError("spreadsheet_source_mismatch:columns")
                    expected = sum(
                        Decimal(source[amount])
                        for source in source_rows
                        if source[category].casefold() == match[2].casefold()
                    )
                    actual_value = cached[sheet.title][cell.coordinate].value
                    if not isinstance(actual_value, (int, float)) or not math.isclose(
                        float(expected), actual_value, rel_tol=1e-12, abs_tol=1e-12
                    ):
                        raise ValueError("spreadsheet_source_mismatch:cached_total")
    finally:
        formulas.close()
        cached.close()
