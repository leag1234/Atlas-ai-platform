"""POC-R1/R2: budgets and answer-key separation at the paid boundary."""

import unittest
from typing import Any

from evals.execution import Session, messages_for


class ExecutionTests(unittest.TestCase):
    def test_multi_turn_never_injects_expected_answer(self) -> None:
        case = {
            "turns": [
                {"user": "Question"},
                {"assistant_expected_contains": "SECRET KEY"},
                {"user": "Again"},
            ]
        }
        self.assertEqual(
            messages_for(case),
            [
                {"role": "user", "content": "Question"},
                {"role": "user", "content": "Again"},
            ],
        )

    def test_budget_reserves_before_call(self) -> None:
        calls: list[object] = []

        def complete(role: str, messages: list[dict[str, str]]) -> dict[str, Any]:
            calls.append(messages)
            return {"text": "answer", "telemetry": {"cost": 0.049}}

        session = Session(complete, limit=0.05)
        session.call("system", [{"role": "user", "content": "hello"}])
        with self.assertRaisesRegex(ValueError, "run_budget"):
            session.call("system", [{"role": "user", "content": "hello"}])
        self.assertEqual(len(calls), 1)

    def test_judge_strict_and_independent(self) -> None:
        calls: list[object] = []

        def complete(role: str, messages: list[dict[str, str]]) -> dict[str, Any]:
            calls.append(messages)
            return {
                "text": '{"score":true,"supported":true,"meaning_reversed":false,"rationale":"x"}',
                "telemetry": {"cost": 0.001},
            }

        with self.assertRaises(ValueError):
            Session(complete).judge(
                "production", {"id": "E7-test", "input": "q"}, "a", []
            )

    def test_schema_matches_strict_grade(self) -> None:
        import json
        from pathlib import Path
        from evals.execution import Grade

        self.assertEqual(
            json.loads(Path("contracts/eval-grade.schema.json").read_text()),
            Grade.model_json_schema(),
        )

    def test_recorded_history_is_not_mutated_after_call(self) -> None:
        captured: list[object] = []

        def complete(role: str, messages: list[dict[str, str]]) -> dict[str, Any]:
            captured.append(messages)
            return {"text": "real answer", "telemetry": {"cost": 0.001}}

        Session(complete).answer({"turns": [{"user": "first"}, {"user": "second"}]})
        self.assertEqual(captured[0], [{"role": "user", "content": "first"}])

    def test_extract_first_object_without_changing_grade(self) -> None:
        from evals.execution import parse_grade

        raw = '{"score":4,"supported":true,"meaning_reversed":false,"rationale":"évidence {ok}"}'
        for text in (raw, "```json\n" + raw + "\n```", '{"{' + raw):
            self.assertEqual(parse_grade(text).score, 4)
        for text in ("no JSON", '{"score":99}', '{"score":true}', "{} " + raw):
            with self.assertRaises(ValueError):
                parse_grade(text)

    def test_refusal_judge_knows_protocol_without_rewriting_answer(self) -> None:
        captured: list[dict[str, str]] = []

        def complete(role: str, messages: list[dict[str, str]]) -> dict[str, Any]:
            captured.extend(messages)
            return {
                "text": '{"score":2,"supported":false,"meaning_reversed":false,"rationale":"counterexample"}',
                "telemetry": {"cost": 0.001},
            }

        grade = Session(complete).judge(
            "production", {"id": "E3-test", "input": "q"}, "INSUFFICIENT", []
        )
        self.assertEqual(grade.score, 2)
        self.assertIn("zero invented facts", captured[0]["content"])
        self.assertIn('"answer": "INSUFFICIENT"', captured[1]["content"])

    def test_translation_language_is_unambiguous(self) -> None:
        messages = messages_for({"direction": "es->it", "input_text": "public text"})
        self.assertIn("into Italian", messages[0]["content"])
        self.assertNotIn("into it.", messages[0]["content"])
