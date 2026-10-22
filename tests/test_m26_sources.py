"""M26 citation provenance and mutable product facts, including terse requests."""

import unittest
from services.orchestrator.search_policy import required_research
from services.orchestrator.source_truth import without_unretrieved


class SourceTests(unittest.TestCase):
    def test_existing_things_trigger_discovery(self) -> None:
        for text, language in (
            ("Quelles caractéristiques offre cet appareil ?", "fr"),
            ("CARACTERISTIQUES APPAREIL", "fr"),
            ("Cette version fonctionne-t-elle encore ?", "fr"),
            ("Which features does this product support?", "en"),
            ("Welche Software unterstützt das?", "de"),
            ("Compare ces deux dispositifs", "fr"),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    required_research(text, language)[0].name, "web_search"
                )
        self.assertEqual(required_research("Imagine a fictional product", "en"), ())
        self.assertEqual(required_research("Compute 2 + 2", "en"), ())

    def test_only_exact_retrieved_urls_survive(self) -> None:
        text = "[Known](https://example.org/page) [Invented](https://example.org/page-fake)"
        result = without_unretrieved(text, {"https://example.org/page"})
        self.assertIn("https://example.org/page)", result)
        self.assertNotIn("page-fake", result)
        self.assertNotIn(
            "Consulté", without_unretrieved("Consulté le 2024-05-23\nAnswer", set())
        )

    def test_retrieval_trail_contains_only_recorded_links(self) -> None:
        from services.orchestrator.source_truth import source_footer

        self.assertEqual(source_footer([], "fr"), "")
        link = "https://example.org/retrieved"
        text = source_footer([link, link], "fr")
        self.assertEqual(text.count(link), 1)
        self.assertIn("<details>", text)
        self.assertIn("Recherche", text)
