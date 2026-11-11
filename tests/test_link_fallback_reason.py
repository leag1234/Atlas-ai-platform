import unittest

from services.orchestrator.link_requests import (
    REASONS,
    fallback_terms,
    reason,
    unavailable,
)


class LinkFallbackTests(unittest.TestCase):
    def test_without_product_names_the_terms_are_the_users_words(self) -> None:
        terms = fallback_terms(
            "Trouve-moi la page officielle du festival de Lorient https://x.example"
        )
        self.assertEqual(terms, "Trouve-moi la page officielle du festival de Lorient")
        self.assertNotIn("requested product", unavailable(terms, "fr"))

    def test_product_names_are_still_extracted(self) -> None:
        self.assertEqual(fallback_terms("Where can I buy a Shure SM7B?"), "Shure SM7B")

    def test_every_cause_is_said_in_every_language(self) -> None:
        for code, texts in REASONS.items():
            self.assertEqual(set(texts), {"fr", "de", "en", "es", "it"}, code)
        self.assertIn("limite", reason("daily_search_limit", "fr"))
        self.assertEqual(
            reason("daily_search_limit", "pt"), REASONS["daily_search_limit"]["en"]
        )
        self.assertEqual(reason("unknown_code", "fr"), "")


if __name__ == "__main__":
    unittest.main()
