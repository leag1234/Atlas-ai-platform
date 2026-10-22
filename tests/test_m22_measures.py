"""Owner J34 ruling: labeled quantities, independent of opening vocabulary."""

import unittest

from m22_gate import assert_price_measures


class PriceMeasureTests(unittest.TestCase):
    def test_distinct_labeled_values_in_prose_and_table(self) -> None:
        for opening in (
            "ouverture",
            "opening",
            "premier cours",
            "premier trade",
            "premier échange",
            "début de cotation",
            "début de séance",
            "first trade",
        ):
            with self.subTest(opening=opening):
                assert_price_measures(f"Prix d'offre : 135 $. {opening} : 150 $.")
                assert_price_measures(
                    f"| Prix d'offre | 135,00 $ |\n| {opening} | ~150 $ |"
                )

    def test_measure_date_does_not_break_value_association(self) -> None:
        assert_price_measures(
            "Prix de l'offre : 135,00 $ par action.\n"
            "À l'ouverture du marché le 12 juin 2026, l'action a débuté à 150 $."
        )

    def test_unlabeled_swapped_missing_and_unrelated_values_fail(self) -> None:
        for answer in (
            "Offre et ouverture. 135 et 150.",
            "Offre : 150 $. Premier trade : 135 $.",
            "Offre : 135 $. Premier trade inconnu.",
            "Offre : 135 $. Premier trade : 135 $. Volume : 150 actions.",
            "Offre : 135 $. Volume : 150 actions. Ouverture inconnue.",
        ):
            with self.subTest(answer=answer), self.assertRaises(AssertionError):
                assert_price_measures(answer)
