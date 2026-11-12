"""The source guard leaves URLs that are part of code intact (staging 2026-11-12:
an SVG logo's xmlns became "[unverified source]" and the copied file broke)."""

import unittest

from services.orchestrator.source_truth import without_unretrieved

SVG = (
    "Here is the logo.\n\n```svg\n"
    '<svg width="400" height="400" xmlns="http://www.w3.org/2000/svg">\n'
    '  <circle cx="200" cy="200" r="190" fill="#FDF6E3" />\n'
    "</svg>\n```\n"
)


class CodeUrlTests(unittest.TestCase):
    def test_namespace_in_fenced_svg_is_kept(self) -> None:
        cleaned = without_unretrieved(SVG, set())
        self.assertIn('xmlns="http://www.w3.org/2000/svg"', cleaned)
        self.assertNotIn("unverified source", cleaned)

    def test_endpoint_in_fenced_example_is_kept(self) -> None:
        text = "Try:\n\n```bash\ncurl https://api.example.test/v1/items\n```\n"
        self.assertIn(
            "https://api.example.test/v1/items", without_unretrieved(text, set())
        )

    def test_inline_svg_namespace_outside_fence_is_kept(self) -> None:
        text = 'Paste <svg xmlns="http://www.w3.org/2000/svg"></svg> in the page.'
        self.assertIn("http://www.w3.org/2000/svg", without_unretrieved(text, set()))

    def test_unretrieved_citation_in_prose_is_still_removed(self) -> None:
        text = SVG + "\nSource: https://invented.example/logo-history\n"
        cleaned = without_unretrieved(text, set())
        self.assertNotIn("https://invented.example/logo-history", cleaned)
        self.assertIn('xmlns="http://www.w3.org/2000/svg"', cleaned)

    def test_inline_code_citation_stays_under_the_guard(self) -> None:
        cleaned = without_unretrieved(
            "See `https://invented.example/page` for details.", set()
        )
        self.assertNotIn("https://invented.example/page", cleaned)

    def test_mermaid_behaviour_is_unchanged(self) -> None:
        text = "```mermaid\nflowchart LR\n  A[https://invented.example/x] --> B\n```\n"
        cleaned = without_unretrieved(text, set())
        self.assertNotIn("https://invented.example/x", cleaned)
        self.assertIn("unverified source", cleaned)


if __name__ == "__main__":
    unittest.main()
