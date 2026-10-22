"""A useful seven-file comparison must retain rows, headers and source quantities."""

import copy
import gzip
import json
import re
from pathlib import Path
from typing import Any
import unittest

from m24_artifacts import validate


class FormatComparisonTests(unittest.TestCase):
    def case(self) -> dict[str, Any]:
        case: dict[str, Any] = json.loads(
            gzip.decompress(Path("tests/cassettes/m24.json.gz").read_bytes())
        )["formats_read"]
        return case

    def test_recorded_complete_table_is_useful_without_capitalized_sheet_name(
        self,
    ) -> None:
        case = self.case()
        validate("formats_read", case, case["response"])

    def test_missing_table_or_wrong_table_quantity_is_rejected(self) -> None:
        for corruption in ("missing", "quantity", "article", "headers"):
            case = copy.deepcopy(self.case())
            message = case["response"]["choices"][0]["message"]
            row = next(
                line
                for line in message["content"].splitlines()
                if line.startswith("|")
                and "inventaire.ods" in line
                and any(
                    cell.strip().strip("`*").casefold() == "abricot"
                    for cell in line.strip("|").split("|")
                )
            )
            self.assertRegex(row, r"\b43\b")
            original = message["content"]
            if corruption == "missing":
                replacement = ""
            elif corruption == "quantity":
                replacement = re.sub(r"\b43\b", "42", row)
            elif corruption == "article":
                replacement = re.sub("abricot", "incorrect", row, flags=re.I)
            else:
                message["content"] = message["content"].replace("Quantité", "Autre")
                replacement = row
            message["content"] = message["content"].replace(row, replacement)
            self.assertNotEqual(message["content"], original)
            with self.subTest(corruption=corruption), self.assertRaises(AssertionError):
                validate("formats_read", case, case["response"])
