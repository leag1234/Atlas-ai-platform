import unittest
from dataclasses import dataclass

from packages.language import conversation_language


@dataclass
class Message:
    role: str
    text: str


class ShortMessageLanguageTests(unittest.TestCase):
    """A few words fool the statistical detector; function words decide first."""

    def test_short_messages_answer_in_their_own_language(self) -> None:
        cases = [
            ("Explain quantum entanglement", "en", "en"),
            ("Explain quantum entanglement", "fr", "en"),
            ("Write a haiku", "fr", "en"),
            ("Define entropy", "en", "en"),
            ("Qué hora es", "en", "es"),
            ("Erkläre die Photosynthese", "en", "de"),
            ("Was ist Entropie", "fr", "de"),
            ("Explique la photosynthèse", "en", "fr"),
            ("Résume cet article", "en", "fr"),
            ("Spiega la fotosintesi", "en", "it"),
            ("Merci beaucoup", "en", "fr"),
            ("Thanks a lot", "fr", "en"),
        ]
        for text, locale, expected in cases:
            with self.subTest(text=text, locale=locale):
                self.assertEqual(
                    conversation_language([Message("user", text)], locale), expected
                )

    def test_without_evidence_the_ui_locale_wins(self) -> None:
        self.assertEqual(conversation_language([Message("user", "ok")], "fr"), "fr")
        self.assertEqual(conversation_language([Message("user", "ok")], "de"), "de")


if __name__ == "__main__":
    unittest.main()
