"""The CI unit parts cover every test module exactly once and stay balanced."""

import json
import statistics
import unittest
from pathlib import Path

import unit_part


class UnitPartTests(unittest.TestCase):
    def test_every_module_runs_in_exactly_one_part(self) -> None:
        for count in (1, 2, 3, 4):
            with self.subTest(count=count):
                assigned = [name for part in unit_part.parts(count) for name in part]
                self.assertEqual(sorted(assigned), unit_part.modules())

    def test_parts_are_balanced(self) -> None:
        weights = json.loads(
            (Path(unit_part.__file__).parent / "unit_weights.json").read_text()
        )
        median = statistics.median(weights.values())
        loads = [
            sum(weights.get(name, median) for name in part)
            for part in unit_part.parts(3)
        ]
        self.assertLess(max(loads) / min(loads), 1.2, loads)
