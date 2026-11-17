"""Defects seen live on staging on 2026-11-17 (judge budget, list numbers)."""

import unittest

from services.orchestrator import source_faithfulness


class LiveDefectTests(unittest.TestCase):
    def test_the_judge_asks_no_more_than_the_assistant_output_limit(self) -> None:
        self.assertLessEqual(source_faithfulness.JUDGE_TOKENS, 6_000)

    def test_list_numbers_are_not_facts_to_find(self) -> None:
        text = "The cathedral was begun in 1248 and completed in 1880."
        source = source_faithfulness.Source(text=text, passages=[text], own=[])
        answer = (
            "Voici les points :\n1. Début en 1248.\n2) Arrêt du chantier.\n"
            "**3.** Achèvement en 1880.\n4. Fin."
        )
        self.assertEqual(source_faithfulness.hints(answer, source), [])

    def test_a_missing_number_is_still_reported(self) -> None:
        text = "The cathedral was begun in 1248 and completed in 1880."
        source = source_faithfulness.Source(text=text, passages=[text], own=[])
        found = source_faithfulness.hints("1. Achèvement en 1890.", source)
        self.assertEqual(found, ["1890"])


if __name__ == "__main__":
    unittest.main()
