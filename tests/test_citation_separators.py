"""A citation removed from a list takes its separator with it."""

import time
import unittest

from services.orchestrator.source_truth import (
    citation_separators,
    clean_citation_residue,
    inline_source_markers,
    without_unretrieved,
)

KEPT = "https://weather.example/caen"
GONE = "https://news.example/forecast"
ALSO_GONE = "https://wiki.example/normandy"


def rendered(text: str) -> str:
    """The streamed answer's citation steps, in chat_stream's order."""
    kept = without_unretrieved(text, {KEPT})
    return inline_source_markers(clean_citation_residue(kept), [KEPT])


class CitationSeparatorTests(unittest.TestCase):
    def test_removed_citations_leave_no_separator(self) -> None:
        link = f"[1]({KEPT})"
        cases = [
            (
                f"Sunny ([a]({KEPT}), [b]({GONE}), [c]({ALSO_GONE})).",
                f"Sunny ({link}).",
            ),
            (
                f"Sunny ([a]({KEPT}),[b]({GONE}),[c]({ALSO_GONE})) tomorrow.",
                f"Sunny ({link}) tomorrow.",
            ),
            (f"Sunny [b]({GONE}), [a]({KEPT}), [c]({ALSO_GONE}).", f"Sunny {link}."),
            (f"Sunny ([b]({GONE}); [a]({KEPT})).", f"Sunny ({link})."),
            (f"Sunny ([b]({GONE}), [c]({ALSO_GONE})).", "Sunny."),
            (
                f"Sunny ([a]({KEPT}), [b]({GONE})), and mild.",
                f"Sunny ({link}), and mild.",
            ),
            (
                f"Sources: [Weather]({KEPT}), [News]({GONE}), [Wiki]({ALSO_GONE})",
                f"Sources: {link}",
            ),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(rendered(text), expected)

    def test_untouched_answers_keep_their_punctuation(self) -> None:
        text = f"Sunny ([a]({KEPT}), [b]({KEPT})), as in print() or f(a, b)."
        self.assertEqual(without_unretrieved(text, {KEPT}), text)

    def test_code_and_function_calls_are_not_repaired(self) -> None:
        text = f"Call print() first ([b]({GONE})).\n```\nrow = f(a,, b)\n```"
        self.assertEqual(
            without_unretrieved(text, {KEPT}),
            "Call print() first.\n```\nrow = f(a,, b)\n```",
        )

    def test_repair_is_linear(self) -> None:
        for text in (" " * 50_000 + "x", "," * 50_000, "( ," * 20_000, " ;" * 30_000):
            started = time.perf_counter()
            citation_separators(text)
            self.assertLess(time.perf_counter() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
