"""Offline checks for precise-claim grounding with fictional data."""

import unittest

from services.orchestrator.grounding import (
    WORDING,
    Grounding,
    extract_claims,
    factual_query,
)


class GroundingTests(unittest.TestCase):
    def test_extracts_dates_numbers_and_proper_names(self) -> None:
        claims = extract_claims(
            "Northbridge opened in 1924. It cost 12 million units. "
            'The novel "Winter Glass" was written by Aster Bell.'
        )
        self.assertEqual(len(claims), 3)
        self.assertIn("1924", claims[0].values)
        self.assertIn("12 million", claims[1].values)
        self.assertIn("winter glass", claims[2].values)
        self.assertIn("aster", claims[2].values)
        self.assertIn("bell", claims[2].values)

    def test_supported_claim_is_unchanged(self) -> None:
        checker = Grounding()
        checker.add("Northbridge opened in 1924 and cost 12 million units.")
        answer = "Northbridge opened in 1924 and cost 12 million units."
        self.assertEqual(checker.check(answer, "Opening?", "en"), answer)
        self.assertEqual(checker.claims_checked, 1)
        self.assertEqual(checker.claims_changed, 0)

    def test_unsupported_claim_without_evidence_is_dropped(self) -> None:
        checker = Grounding()
        answer = checker.check("Northbridge opened in 1924.", "Opening?", "en")
        self.assertNotIn("Northbridge opened in 1924", answer)
        self.assertIn("cannot verify", answer)
        self.assertEqual(checker.claims_changed, 1)

    def test_unsupported_claim_with_evidence_is_marked_not_quoted_as_fact(self) -> None:
        checker = Grounding()
        checker.add("Northbridge opened in 1924.")
        answer = checker.check("Northbridge opened in 1925.", "Opening?", "en")
        self.assertIn("Unverified claim", answer)
        self.assertIn("> Northbridge opened in 1925.", answer)

    def test_no_sources_and_no_precise_claims_are_unchanged(self) -> None:
        checker = Grounding()
        answer = "The two fictional places are described differently."
        self.assertEqual(checker.check(answer, "Compare them.", "en"), answer)
        self.assertEqual(checker.claims_checked, 0)

    def test_code_blocks_are_unchanged(self) -> None:
        checker = Grounding()
        answer = "Here is the function:\n```python\nvalue = 1924\n```"
        self.assertEqual(checker.check(answer, "Write a function.", "en"), answer)
        self.assertEqual(checker.claims_checked, 0)

    def test_false_premise_is_corrected_from_retrieved_source(self) -> None:
        checker = Grounding()
        checker.add("Northbridge opened in 1924.")
        answer = checker.check(
            "Northbridge opened in 1924, not 1912.",
            "Why was Northbridge opened in 1912?",
            "en",
        )
        self.assertIn("1924, not 1912", answer)
        self.assertIn("> Northbridge opened in 1924.", answer)
        self.assertEqual(checker.premises_checked, 1)

    def test_premise_search_is_neutral(self) -> None:
        self.assertEqual(
            factual_query("Why was Northbridge opened in 1912?"),
            "was Northbridge opened in ?",
        )

    def test_creative_request_does_not_trigger_search(self) -> None:
        self.assertEqual(factual_query('Translate "Winter Glass"'), "")

    def test_wording_exists_in_every_ui_language(self) -> None:
        for key, table in WORDING.items():
            self.assertEqual(set(table), {"en", "fr", "de", "es", "it"}, key)

    def test_unsupported_claim_uses_the_answer_language(self) -> None:
        answer = "Northbridge opened in 1924."
        for language in ("en", "fr", "de", "es", "it"):
            checked = Grounding().check(answer, "When did Northbridge open?", language)
            self.assertIn(WORDING["missing"][language], checked)
            self.assertNotIn("1924", checked)
        checked = Grounding().check(answer, "When did Northbridge open?", "pt")
        self.assertIn(WORDING["missing"]["en"], checked)


if __name__ == "__main__":
    unittest.main()
