"""The source guard keeps URLs the user wrote (lot of fixes 2026-11-09)."""

import unittest

from services.orchestrator.source_truth import user_urls, without_unretrieved


class UserUrlTests(unittest.TestCase):
    def test_user_supplied_url_is_kept_and_unknown_url_is_not(self) -> None:
        text = "Votre page https://example.org/docs est citee ; voir aussi https://invented.example/x."
        token = user_urls.set(frozenset({"https://example.org/docs"}))
        try:
            cleaned = without_unretrieved(text, set())
        finally:
            user_urls.reset(token)
        self.assertIn("https://example.org/docs", cleaned)
        self.assertNotIn("https://invented.example/x", cleaned)

    def test_without_user_urls_the_guard_is_unchanged(self) -> None:
        cleaned = without_unretrieved("Voir https://example.org/docs.", set())
        self.assertNotIn("https://example.org/docs", cleaned)
