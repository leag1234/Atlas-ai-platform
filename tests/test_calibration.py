"""POC-R2: paired independent grades, including undefined agreement."""

import unittest
from typing import Any

from evals.calibration import agreement, calibrate


class CalibrationTests(unittest.TestCase):
    def test_known_cohen_example(self) -> None:
        result = agreement([1, 1, 2, 2], [1, 2, 2, 2])
        self.assertAlmostEqual(result["kappa"], 0.5)
        self.assertEqual(result["agreement"], 0.75)
        self.assertEqual(result["mean_absolute_difference"], 0.25)

    def test_invalid_and_degenerate(self) -> None:
        invalid: list[tuple[list[Any], list[Any]]] = [
            ([], []),
            ([1], []),
            ([0], [1]),
            ([True], [1]),
            ([1.5], [2]),
            ([1, 1], [1, 1]),
        ]
        for left, right in invalid:
            with self.subTest(left=left, right=right), self.assertRaises(ValueError):
                agreement(left, right)

    def test_language_coverage_and_unique_pairs(self) -> None:
        pairs = [
            {
                "id": f"{lang}-{i}",
                "lang": lang,
                "production": i % 5 + 1,
                "reference": i % 5 + 1,
            }
            for lang in ("fr", "de", "es", "it", "en")
            for i in range(6)
        ]
        result = calibrate(pairs)
        self.assertEqual(result["kappa"], 1.0)
        self.assertEqual(len(result["par_langue"]), 5)
        for invalid in (pairs[:-1], pairs + [pairs[0]], pairs[:24]):
            with self.assertRaises(ValueError):
                calibrate(invalid)

    def test_best_effort_reports_exclusion_and_undefined_language(self) -> None:
        from evals.calibration import best_effort

        pairs = [
            {"id": "a", "lang": "fr", "production": 5, "reference": 5},
            {"id": "b", "lang": "en", "production": 4, "reference": 5},
        ]
        result = best_effort(pairs, [{"id": "c", "reason": "unparseable_grade"}])
        self.assertEqual(result["sample_size"], 2)
        self.assertEqual(result["kappa"], 0)
        self.assertIsNone(result["par_langue"]["fr"]["kappa"])
        self.assertEqual(result["excluded"][0]["id"], "c")
        with self.assertRaises(ValueError):
            best_effort(pairs + [pairs[0]], [])
