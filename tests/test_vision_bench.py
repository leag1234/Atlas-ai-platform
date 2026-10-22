"""M22 scorer cannot earn credit for unordered, missing or fabricated positions."""

import json
from pathlib import Path
import unittest
from vision_bench.score import score


class VisionBenchTests(unittest.TestCase):
    def test_annotated_string_order_and_partial_score(self) -> None:
        annotation = json.loads(Path("tests/vision_bench/guitar.json").read_text())
        result = score(
            json.dumps(
                {
                    "positions": [5, 5, 7, 5, 5, 7],
                    "full_barre_fret": 5,
                    "chord_family": "A minor",
                }
            ),
            annotation,
        )
        self.assertEqual(
            result,
            {
                "positions_correct": 6,
                "positions_total": 6,
                "structure_recognised": True,
                "chord_family_compatible": True,
            },
        )
        result = score(
            json.dumps(
                {
                    "positions": [7, 5, None, 5, 7, 5],
                    "full_barre_fret": None,
                    "chord_family": "unknown",
                }
            ),
            annotation,
        )
        fenced = (
            "```json\n"
            + json.dumps(
                {
                    "positions": [7, 5, None, 5, 7, 5],
                    "full_barre_fret": None,
                    "chord_family": "unknown",
                }
            )
            + "\n```"
        )
        self.assertEqual(score(fenced, annotation), result)
        self.assertEqual(result["positions_correct"], 2)
        self.assertFalse(result["structure_recognised"])
        self.assertFalse(result["chord_family_compatible"])
        for positions in ([5] * 5, [True] * 6, ["5"] * 6, [99] * 6):
            with self.assertRaises(ValueError):
                score(
                    json.dumps(
                        {
                            "positions": positions,
                            "full_barre_fret": 5,
                            "chord_family": "A minor",
                        }
                    ),
                    annotation,
                )
        self.assertEqual(
            Path("tests/vision_bench/guitar.jpg").read_bytes(),
            Path("tests/journeys/m21-guitar.jpg").read_bytes(),
        )
