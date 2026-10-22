"""CI overlap preserves prerequisites, all gates and failures in either lane."""

import threading
import unittest

from ci_regressions import run


class CIRegressionTests(unittest.TestCase):
    def test_independent_gates_overlap_and_report_waits_for_both(self) -> None:
        evaluation_started = threading.Event()
        journeys_finished = threading.Event()
        completed: list[int] = []

        def execute(milestone: int) -> None:
            if milestone == 5:
                evaluation_started.set()
                self.assertTrue(journeys_finished.wait(2))
            elif milestone == 7:
                self.assertTrue(evaluation_started.wait(2))
            elif milestone == 21:
                journeys_finished.set()
            elif milestone == 6:
                self.assertEqual(set(completed), {5, *range(7, 27)})
            completed.append(milestone)

        run(execute)
        self.assertEqual(sorted(completed), list(range(5, 27)))
        self.assertEqual(completed[-1], 6)

    def test_failure_in_either_lane_prevents_report_success(self) -> None:
        for failed in (5, 12, 24, 25, 26):
            completed = []

            def execute(milestone: int) -> None:
                if milestone == failed:
                    raise RuntimeError("gate_failed")
                completed.append(milestone)

            with self.subTest(failed=failed):
                with self.assertRaisesRegex(RuntimeError, "gate_failed"):
                    run(execute)
                self.assertNotIn(6, completed)
