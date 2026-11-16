"""Characters the document face lacks are adapted, not refused (staging
2026-11-12: a Word report on electric cars with "CO₂" was never delivered).

Emoji are drawn from the colour emoji font when the system has it (staging
2026-11-16: a picture book lost every emoji); without it they are left out and
named by left_out, never dropped in silence.
"""

import os
import unittest
from unittest import mock

from packages.doc_render import emoji
from packages.doc_render.glyphs import adapt, covered, left_out
from packages.doc_render.schema import Content


class WithoutEmojiFont(unittest.TestCase):
    """The behaviour on a system without a colour emoji font."""

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {"ATLAS_EMOJI_FONT": ""})
        patcher.start()
        emoji.reset()
        self.addCleanup(emoji.reset)
        self.addCleanup(patcher.stop)


def drawable(text: str) -> bool:
    return all(ord(character) in covered() for character in text)


class GlyphTests(WithoutEmojiFont):
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

    def test_dropped_characters_are_named(self) -> None:
        # Equivalents are not losses; a dropped emoji or symbol is.
        self.assertEqual(left_out("🐔 poule, 🍎 pomme → ✓ 🐔 ⚠️"), ["🐔", "🍎", "⚠"])
        self.assertEqual(left_out("CO₂ → 2030 ✓ « ok »\u200b"), [])

    def test_drawable_text_is_unchanged(self) -> None:
        text = "x² + y³ ≈ 10 ≤ 12, « citation » — fin…"
        self.assertEqual(adapt(text), text)

    def test_any_arrow_bullet_shape_and_fraction_is_adapted(self) -> None:
        # Staging -27 (2026-11-12): a Word report still refused for symbols the
        # first version did not list.
        self.assertEqual(
            adapt("Suite ⇨ fin ➜ ok ⟹ donc ; ▪ point ; ► titre ; ⅓ ; ⚠️ note"),
            "Suite -> fin -> ok -> donc ; - point ; - titre ; 1/3 ; note",
        )
        self.assertEqual(adapt("mot\u2003mot"), "mot mot")
        for text in ("✱ ℹ ⌘ ⏱ ♫ ☂", "a\u200bb"):
            self.assertTrue(drawable(adapt(text)), adapt(text))

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


@unittest.skipUnless(emoji.font_path(), "no colour emoji font on this system")
class EmojiFontTests(unittest.TestCase):
    def setUp(self) -> None:
        emoji.reset()

    def test_emoji_are_kept_and_text_symbols_still_adapted(self) -> None:
        self.assertEqual(
            adapt("⛵ Voilier → 🐔 Poule ✓ ⚠️ 👨\u200d👩\u200d👧 🇫🇷"),
            "⛵ Voilier -> 🐔 Poule + ⚠️ 👨\u200d👩\u200d👧 🇫🇷",
        )
        self.assertEqual(left_out("⛵ 🐔 👍🏽 🇫🇷"), [])

    def test_sequences_are_one_picture(self) -> None:
        self.assertEqual(
            emoji.tokens("a👨\u200d👩\u200d👧b👍🏽🇫🇷"),
            ["a", "👨\u200d👩\u200d👧", "b", "👍🏽", "🇫🇷"],
        )
        family = emoji.picture("👨\u200d👩\u200d👧")
        self.assertIsNotNone(family)
        assert family is not None
        self.assertTrue(family.png.startswith(b"\x89PNG"))
        self.assertGreater(family.advance, 0.9)
        self.assertLess(family.advance, 1.6)

    def test_an_arrow_or_check_mark_is_not_an_emoji(self) -> None:
        self.assertEqual([t for t in emoji.tokens("→ ✓ ➜ 1️⃣") if emoji.is_emoji(t)], [])


if __name__ == "__main__":
    unittest.main()
