"""Spreadsheet totals must be correct whether stored as values or formulas."""

import base64
import copy
import gzip
import io
import json
from pathlib import Path
import unittest
from typing import Any
from zipfile import ZipFile
from xml.etree import ElementTree as ET

from m24_artifacts import spreadsheet_rows, validate


class SpreadsheetTotalsTests(unittest.TestCase):
    def case(self) -> dict[str, Any]:
        case: dict[str, Any] = json.loads(
            gzip.decompress(Path("tests/cassettes/m24.json.gz").read_bytes())
        )["J44"]
        return case

    def test_recorded_totals_are_computed(self) -> None:
        case = self.case()
        validate("J44", case, case["response"])

    def test_incorrect_total_is_rejected(self) -> None:
        case = copy.deepcopy(self.case())
        artifact = case["artifacts"][0]
        output = io.BytesIO()
        with (
            ZipFile(io.BytesIO(base64.b64decode(artifact["bytes"]))) as source,
            ZipFile(output, "w") as target,
        ):
            for name in source.namelist():
                data = source.read(name)
                if name == "xl/worksheets/sheet2.xml":
                    sheet = ET.fromstring(data)
                    ns = {
                        "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                    }
                    row = next(
                        row
                        for row in sheet.findall(".//s:row", ns)
                        if "Total" in "".join(row.itertext())
                    )
                    cell = row.findall("s:c", ns)[1]
                    for child in list(cell):
                        cell.remove(child)
                    ET.SubElement(cell, "{" + ns["s"] + "}v").text = "199"
                    data = ET.tostring(sheet)
                target.writestr(name, data)
        artifact["bytes"] = base64.b64encode(output.getvalue()).decode()
        with self.assertRaisesRegex(AssertionError, "incorrect_march_totals"):
            validate("J44", case, case["response"])

    def test_sum_ranges_and_cycles(self) -> None:
        sheet = ET.fromstring(
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row><c r="B4"><v>80</v></c></row>'
            '<row><c r="B5"><v>120</v></c></row>'
            '<row><c r="B6"><f>SUM(B4:B5)</f></c></row></sheetData></worksheet>'
        )
        self.assertEqual(spreadsheet_rows(sheet)[-1], ["200"])
        formula = next(node for node in sheet.iter() if node.tag.endswith("}f"))
        formula.text = "SUM(B4:B4)"
        self.assertEqual(spreadsheet_rows(sheet)[-1], ["80"])
        formula.text = "SUM(B6:B6)"
        with self.assertRaisesRegex(AssertionError, "cyclic_formula"):
            spreadsheet_rows(sheet)

    def test_arithmetic_and_conditional_formulas(self) -> None:
        sheet = ET.fromstring(
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row><c r="B1"><v>200</v></c>'
            '<c r="C1"><v>260</v></c><c r="D1"><f>C1-B1</f></c>'
            '<c r="E1"><f>IF(B1=0,"N/A",(C1-B1)/B1)</f></c>'
            "</row></sheetData></worksheet>"
        )
        self.assertEqual(spreadsheet_rows(sheet), [["200", "260", "60", "0.3"]])
        cells = list(sheet.iter())[3:]
        number = next(n for n in cells if n.tag.endswith("}v"))
        number.text = "0"
        self.assertEqual(spreadsheet_rows(sheet)[0][-1], "N/A")
        formula = next(n for n in sheet.iter() if n.tag.endswith("}f"))
        for expression, error in (
            ("D1-B1", "cyclic_formula"),
            ('__import__("os")', "unsupported_spreadsheet_formula"),
        ):
            formula.text = expression
            with (
                self.subTest(expression=expression),
                self.assertRaisesRegex(AssertionError, error),
            ):
                spreadsheet_rows(sheet)
