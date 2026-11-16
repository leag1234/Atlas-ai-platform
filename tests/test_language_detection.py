"""The answer follows the user's language for short, unaccented and SMS-style messages.

The measured set is tests/fixtures/language-detection.json; tools/language_eval.py
prints the same measurement with every miss.
"""

import importlib.util
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.language import (
    LANGUAGES,
    conversation_language,
    detected_language,
    identified_language,
    language_scores,
    prose_language,
    word_count,
)


@dataclass
class Message:
    role: str
    text: str


def answer_language(texts: list[str], locale: str) -> str:
    messages = [
        Message("user" if index % 2 == 0 else "assistant", text)
        for index, text in enumerate(texts)
    ]
    return prose_language(messages, conversation_language(messages, locale))


def evaluation() -> Any:
    path = Path(__file__).resolve().parents[1] / "tools" / "language_eval.py"
    spec = importlib.util.spec_from_file_location("language_eval", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["language_eval"] = module
    spec.loader.exec_module(module)
    return module


class StagingCaseTests(unittest.TestCase):
    def test_unaccented_french_is_not_welsh(self) -> None:
        from services.orchestrator.chat_schema import ChatRequest

        for locale in ("fr", "en"):
            with self.subTest(locale=locale):
                request = ChatRequest.model_validate(
                    {
                        "messages": [
                            {"role": "user", "content": "il fera beau demain a caen?"}
                        ],
                        "ui_locale": locale,
                    }
                )
                self.assertEqual(request.answer_language, "fr")
                request = request.model_copy(update={"lang": request.answer_language})
                self.assertEqual(request.prose_lang, "fr")

    def test_search_runs_in_the_question_language(self) -> None:
        from packages.images import VisionMessage
        from services.orchestrator.searxng import search_language

        message = VisionMessage(role="user", content="il fera beau demain a caen?")
        self.assertEqual(search_language([message], "en"), "fr")


class ShortMessageTests(unittest.TestCase):
    def test_sms_style_and_typos_in_the_five_languages(self) -> None:
        cases = [
            ("c koi la capitale de l australie", "fr"),
            ("jsp quoi faire ce soir", "fr"),
            ("tkt mais tu peux me resumer ca", "fr"),
            ("idk what to cook tonight", "en"),
            ("wat r the symptoms of flu", "en"),
            ("wann faehrt der naechste zug nach berlin", "de"),
            ("q hora es en mexico", "es"),
            ("xq no funciona mi wifi", "es"),
            ("xke il mio wifi e lento", "it"),
            ("nn ho capito niente", "it"),
        ]
        for text, expected in cases:
            for locale in ("en", "fr", "de"):
                with self.subTest(text=text, locale=locale):
                    self.assertEqual(answer_language([text], locale), expected)

    def test_capitals_and_missing_punctuation_change_nothing(self) -> None:
        for text in ("il fera beau demain a caen?", "IL FERA BEAU DEMAIN A CAEN"):
            with self.subTest(text=text):
                self.assertEqual(answer_language([text], "en"), "fr")

    def test_a_town_name_alone_keeps_the_interface_language(self) -> None:
        for locale in LANGUAGES:
            with self.subTest(locale=locale):
                self.assertEqual(answer_language(["Caen"], locale), locale)

    def test_detection_is_deterministic(self) -> None:
        text = "demain il neige a grenoble ?"
        self.assertEqual({identified_language(text) for _ in range(20)}, {"fr"})


class OtherLanguageTests(unittest.TestCase):
    def test_a_sentence_in_another_language_is_still_answered_in_it(self) -> None:
        cases = [
            (
                "Beth fydd y tywydd yng Nghaerdydd yfory? Rydw i eisiau mynd am dro "
                "ar hyd yr afon gyda fy nheulu.",
                "cy",
            ),
            ("gaat het morgen regenen in utrecht? ik wil gaan fietsen", "nl"),
            ("vai chover amanha em lisboa? quero ir a praia com os meus filhos", "pt"),
            (
                "Hur lång tid tar det att köra från Stockholm till Göteborg med bil?",
                "sv",
            ),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(answer_language([text], "en"), expected)
                # The interface keeps one of its five languages.
                self.assertEqual(
                    conversation_language([Message("user", text)], "en"), "en"
                )

    def test_another_language_must_win_by_a_clear_margin(self) -> None:
        scores = language_scores("il fera beau demain a caen?", (), "en")
        best_other = max(
            score for language, score in scores.items() if language not in LANGUAGES
        )
        self.assertGreater(scores["fr"], best_other)
        # The conversation's own language needs no such margin.
        self.assertEqual(identified_language("en de prijs dan?", ("nl",)), "nl")

    def test_scripts_settle_han_characters(self) -> None:
        self.assertEqual(
            identified_language("明天北京的天气怎么样？需要带伞吗？"), "zh-cn"
        )
        self.assertEqual(identified_language("明日の東京の天気はどうですか？"), "ja")
        self.assertEqual(word_count("明天北京的天气怎么样？需要带伞吗？"), 7)

    def test_without_letters_there_is_no_language(self) -> None:
        self.assertIsNone(identified_language("123 ?"))
        self.assertIsNone(detected_language(""))


class MeasuredSetTests(unittest.TestCase):
    """Floors under the measurement of 2026-11-16 (before: 85.2 % tune, 92.3 % check)."""

    eval: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.eval = evaluation()

    def test_accuracy_floors(self) -> None:
        tune, check = self.eval.measure("tune"), self.eval.measure("check")
        self.assertGreaterEqual(tune["accuracy"], 0.98)
        self.assertGreaterEqual(check["accuracy"], 0.98)
        self.assertGreaterEqual(tune["by_kind"]["short"]["accuracy"], 0.99)
        self.assertGreaterEqual(tune["interface_accuracy"], 0.99)

    def test_five_language_messages_never_get_another_language(self) -> None:
        misses = self.eval.measure()["misses"]
        self.assertEqual(
            [
                miss
                for miss in misses
                if miss["expected"] in LANGUAGES and miss["got"] not in LANGUAGES
            ],
            [],
        )


if __name__ == "__main__":
    unittest.main()
