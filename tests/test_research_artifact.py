"""Research acceptance checks sourced facts, permitting a primary-page read."""

import copy
import gzip
import json
from pathlib import Path
from typing import Any
import unittest

from m24_artifacts import validate


class ResearchArtifactTests(unittest.TestCase):
    def case(self) -> dict[str, Any]:
        case: dict[str, Any] = json.loads(
            gzip.decompress(Path("tests/cassettes/m24.json.gz").read_bytes())
        )["J48"]
        return case

    def test_search_plus_primary_page_read_is_allowed(self) -> None:
        case = self.case()
        validate("J48", case, case["response"])

    def test_missing_search_or_wrong_timings_fail(self) -> None:
        case = copy.deepcopy(self.case())
        case["exchanges"] = [
            row
            for row in case["exchanges"]
            if row["request"].get("call", {}).get("name") != "web_search"
        ]
        with self.assertRaises(AssertionError):
            validate("J48", case, case["response"])
        case = copy.deepcopy(self.case())
        message = case["response"]["choices"][0]["message"]
        message["content"] = message["content"].replace("5 µs", "4 µs")
        with self.assertRaises(AssertionError):
            validate("J48", case, case["response"])
