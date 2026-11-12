"""Conversation titles (staging 2026-11-12: "is times" for "What is 17 times 23?")."""

import asyncio
import json
import os
import unittest
from decimal import Decimal
from unittest import mock

from services.orchestrator.title import (
    heuristic_title,
    is_title_task,
    title_answer,
    title_response,
    valid_title,
)


def payload(question: str) -> str:
    return (
        "### Task:\nGenerate a concise title summarizing the chat history.\n"
        '### Output:\nJSON format: { "title": "your concise title here" }\n'
        f"### Chat History:\n<chat_history>\nUSER: {question}\nASSISTANT: Sure.\n</chat_history>"
    )


class HeuristicTitleTests(unittest.TestCase):
    def test_numbers_are_kept_and_first_letter_is_capital(self) -> None:
        self.assertEqual(
            heuristic_title("What is 17 times 23?", "en"), "What is 17 times 23"
        )

    def test_polite_opening_is_dropped(self) -> None:
        self.assertEqual(
            heuristic_title("Pouvez-vous créer un logo pour ma boulangerie ?", "fr"),
            "Créer un logo pour ma boulangerie",
        )
        self.assertEqual(
            heuristic_title("can you write a haiku about rain", "en"),
            "Write a haiku about rain",
        )

    def test_long_question_is_cut_to_seven_words(self) -> None:
        title = heuristic_title(
            "make me a drum machine where I can pick a base beat and play", "en"
        )
        self.assertEqual(title, "Make me a drum machine where I")

    def test_empty_question_uses_the_localized_fallback(self) -> None:
        self.assertEqual(heuristic_title("", "fr"), "Nouvelle conversation")

    def test_deterministic_response_is_open_webui_json(self) -> None:
        answer = json.loads(title_response(payload("write a haiku about rain"), "en"))
        self.assertEqual(answer, {"title": "Write a haiku about rain"})

    def test_title_task_detection_is_unchanged(self) -> None:
        self.assertTrue(is_title_task(payload("hello there")))
        self.assertFalse(is_title_task("Generate a concise title for my essay"))


class ModelTitleTests(unittest.TestCase):
    def run_answer(self, question: str, result: object) -> tuple[dict, Decimal]:
        async def complete(body: dict, seconds: float) -> dict:
            if isinstance(result, Exception):
                raise result
            return result  # type: ignore[return-value]

        with mock.patch.dict(os.environ, {"ATLAS_MODEL_TITLES": "1"}):
            with mock.patch(
                "services.orchestrator.request_router.profile", return_value="fast"
            ):
                text, cost = asyncio.run(
                    title_answer(payload(question), "en", complete=complete)
                )
        return json.loads(text), cost

    def test_model_title_is_used_and_costed(self) -> None:
        answer, cost = self.run_answer(
            "What is 17 times 23?",
            {"text": '{"title": "multiplying 17 by 23"}', "cost_eur": "0.00004"},
        )
        self.assertEqual(answer, {"title": "Multiplying 17 by 23"})
        self.assertEqual(cost, Decimal("0.00004"))

    def test_failure_falls_back_to_the_question(self) -> None:
        answer, _ = self.run_answer("What is 17 times 23?", TimeoutError())
        self.assertEqual(answer, {"title": "What is 17 times 23"})

    def test_invalid_model_output_falls_back(self) -> None:
        answer, _ = self.run_answer(
            "write a haiku about rain",
            {
                "text": '{"title": "Rain falls softly on the roof\\nand the night is long and"}'
            },
        )
        self.assertEqual(answer, {"title": "Write a haiku about rain"})

    def test_flag_off_makes_no_call(self) -> None:
        async def complete(body: dict, seconds: float) -> dict:
            raise AssertionError("no call expected")

        with mock.patch.dict(os.environ, {"ATLAS_MODEL_TITLES": ""}):
            text, cost = asyncio.run(
                title_answer(payload("What is 17 times 23?"), "en", complete=complete)
            )
        self.assertEqual(json.loads(text), {"title": "What is 17 times 23"})
        self.assertEqual(cost, Decimal(0))

    def test_valid_title_rules(self) -> None:
        self.assertEqual(valid_title('"crêpes au sucre."'), "Crêpes au sucre")
        self.assertIsNone(valid_title(""))
        self.assertIsNone(valid_title("x" * 80))
        self.assertIsNone(valid_title(None))


if __name__ == "__main__":
    unittest.main()
