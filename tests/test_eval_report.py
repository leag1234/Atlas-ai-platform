"""POC-R1/F8: invalid measurements cannot become a green report."""

import json
import tempfile
import unittest
from pathlib import Path

from evals.reporting import aggregate, publish


class ReportTests(unittest.TestCase):
    def test_gate_missing_duplicate_nonfinite_and_threshold(self) -> None:
        rows = [
            dict(id=f"E{i}", suite=f"e{i}", lang="fr", score=1.0) for i in range(1, 10)
        ]
        self.assertTrue(aggregate(rows)["quality_go"])
        for bad in [
            [],
            rows[:-1],
            rows + rows[:1],
            rows[:1] + [dict(rows[1], score=float("nan"))] + rows[2:],
        ]:
            with self.assertRaises(ValueError):
                aggregate(bad)
        rows[1]["score"] = 0.6
        self.assertFalse(aggregate(rows)["quality_go"])
        rows[1]["score"] = 1.0
        rows.append(dict(id="e2-de", suite="e2", lang="de", score=0.8))
        self.assertFalse(aggregate(rows)["quality_go"])

    def test_persistence_diff_escape_and_unknown_metrics(self) -> None:
        report = aggregate(
            [
                dict(id=f"E{i}", suite=f"e{i}", lang="fr", score=1.0)
                for i in range(1, 10)
            ]
        )
        report["evidence"] = "<script>alert(1)</script>"
        telemetry = [
            dict(
                tokens=2,
                cost=0.001,
                latency=0.2,
                ttft=0.1,
                tok_s=None,
                cache_hit_ratio=None,
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            publish(root, report, telemetry, {"kappa": 0.5})
            publish(root, report, telemetry, {"kappa": 0.5})
            saved = json.loads((root / "report.json").read_text())
            self.assertEqual(saved["diff"]["e2"], 0)
            self.assertIsNone(
                json.loads((root / "telemetry.json").read_text())["cache_hit_ratio"]
            )
            self.assertNotIn("<script>", (root / "report.html").read_text())
            self.assertIn("&lt;script&gt;", (root / "report.html").read_text())
            self.assertEqual(len(list(root.glob("report-*.html"))), 2)
            import sqlite3

            with sqlite3.connect(root / "runs.sqlite") as db:
                self.assertEqual(
                    db.execute("select count(*) from runs").fetchone()[0], 2
                )

    def test_language_target_remains_visible_but_poc_is_indicative(self) -> None:
        rows = [
            dict(id=f"E{i}", suite=f"e{i}", lang="fr", score=1.0) for i in range(1, 10)
        ]
        rows.append(dict(id="e2-de", suite="e2", lang="de", score=0.8))
        result = aggregate(rows)
        self.assertFalse(result["quality_go"])
        self.assertTrue(result["poc_passed"])
        rows[-1]["score"] = 0.1
        self.assertFalse(aggregate(rows)["poc_passed"])
