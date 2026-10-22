import unittest

from evals.decision import percentile, render


class DecisionTests(unittest.TestCase):
    def test_percentile_validates_and_does_not_invent(self) -> None:
        self.assertIsNone(percentile([]))
        self.assertEqual(percentile([1.0, 2.0, 3.0]), 3.0)
        with self.assertRaises(ValueError):
            percentile([float("nan")])

    def test_missing_performance_is_no_go_even_with_good_quality(self) -> None:
        tele = {"latency": 1.0, "ttft": 0.1, "tok_s": 50.0, "cost": 0.001}
        record = {"telemetry": tele}
        bench = {"baseline": record, "waves": [[record] * 8] * 2, "budget_tests": True}
        demo = [
            {"id": ident, "latency": 2.0, "cost": 0.001}
            for ident in ("DEMO-RAG", "DEMO-WEB", "DEMO-FALLBACK")
        ]
        quality = {
            "quality_go": True,
            "suites": {f"e{i}": {"score": 1.0} for i in range(1, 10)},
        }
        output = render(quality, bench, demo, {"kappa": 0.5})
        self.assertIn("NO-GO", output)
        self.assertIn("indisponible", output)
        for i in range(1, 10):
            self.assertIn(f"P{i}", output)
        self.assertIn("E1", output)
        with self.assertRaises(ValueError):
            render({}, bench, demo, {})
