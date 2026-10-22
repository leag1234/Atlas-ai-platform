"""The M10 gate must reject an excuse or a formula for a different quantity."""

import unittest

from large_input_gate import assert_formula


class AnswerChecks(unittest.TestCase):
    def test_rejects_missing_definition_and_single_decrement(self) -> None:
        for answer in (
            "The excerpt does not contain cancelling(). uncancel decrements the count.",
            "Exact subtraction formula: remaining_count = current_count - 1.",
            "cancel increments and uncancel decrements. The formula is not provided.",
        ):
            with self.subTest(answer=answer), self.assertRaises(AssertionError):
                assert_formula(answer)

    def test_accepts_the_requested_difference(self) -> None:
        for answer in (
            "Pending count = number of cancel() calls minus number of uncancel() calls.",
            "**Pending Count = (Number of `cancel()` calls) − (Number of `uncancel()` calls)**",
        ):
            assert_formula(answer)
