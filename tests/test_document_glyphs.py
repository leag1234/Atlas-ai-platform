"""Characters the document face lacks are adapted, not refused (staging
2026-11-12: a Word report on electric cars with "CO₂" was never delivered)."""

import unittest

from packages.doc_render.glyphs import adapt, covered
from packages.doc_render.schema import Content


def drawable(text: str) -> bool:
    return all(ord(character) in covered() for character in text)


class GlyphTests(unittest.TestCase):
    def test_common_characters_get_an_equivalent(self) -> None:
        cases = {
            "Émissions de CO₂ en baisse → objectif 2030 ✓": "Émissions de CO2 en baisse -> objectif 2030 +",
            "Le coefficient α = 0,5 et Δ = 3": "Le coefficient alpha = 0,5 et Delta = 3",
            "Kossuth Lajos utca ő": "Kossuth Lajos utca o",
            "1 250,00 €": "1 250,00 €",
        }
        for given, expected in cases.items():
            with self.subTest(given=given):
                self.assertEqual(adapt(given), expected)
                self.assertTrue(drawable(adapt(given)))

    def test_pictographs_are_dropped_without_double_spaces(self) -> None:
        self.assertEqual(adapt("🚗 Ventes ⚡ électriques 📈"), "Ventes électriques")

    def test_drawable_text_is_unchanged(self) -> None:
        text = "x² + y³ ≈ 10 ≤ 12, « citation » — fin…"
        self.assertEqual(adapt(text), text)

    def test_an_uncovered_script_is_left_for_the_layout_to_refuse(self) -> None:
        self.assertEqual(adapt("中文报告"), "中文报告")

    def test_content_is_adapted_when_validated(self) -> None:
        content = Content.model_validate(
            {
                "title": "Voitures électriques ⚡ en Europe",
                "sections": [
                    {
                        "heading": "Émissions de CO₂",
                        "paragraphs": ["Baisse de 20 % → objectif 2030."],
                        "bullets": ["✓ Normes Euro 7"],
                        "tables": [
                            {
                                "headers": ["Pays", "CO₂ (g/km)"],
                                "rows": [["France", "≈ 95"]],
                            }
                        ],
                    }
                ],
            }
        )
        self.assertTrue(all(drawable(text) for text in content.texts()))
        self.assertEqual(content.sections[0].heading, "Émissions de CO2")
        self.assertEqual(content.sections[0].bullets[0], "+ Normes Euro 7")


if __name__ == "__main__":
    unittest.main()
