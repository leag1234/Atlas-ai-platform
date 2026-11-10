"""Chat UI failures read as one sentence in the user's language; budgets keep their values."""

import unittest

from packages.user_errors import ui_failure


class UserErrorTests(unittest.TestCase):
    def test_provider_failures_and_deadlines_are_readable(self) -> None:
        for code in ("provider_error", "malformed_tool_call", "request_size_exceeded"):
            with self.subTest(code=code):
                message = ui_failure(code, "fr")
                assert message is not None
                self.assertIn("Réessayez", message)
                self.assertNotIn("_", message)
        timeout = ui_failure("timeout", "en")
        assert timeout is not None
        self.assertIn("took too long", timeout)

    def test_unknown_language_falls_back_to_english(self) -> None:
        self.assertEqual(
            ui_failure("provider_error", "pt"), ui_failure("provider_error", "en")
        )

    def test_budget_refusals_keep_their_measured_message(self) -> None:
        for code in (
            "cost_budget",
            "daily_budget",
            "quota_exhausted",
            "context_exceeded",
        ):
            with self.subTest(code=code):
                self.assertIsNone(ui_failure(code, "fr"))


if __name__ == "__main__":
    unittest.main()
