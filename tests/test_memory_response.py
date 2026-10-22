"""Consolidation trusts only validated output grounded in the current user turn."""

import json
import unittest

from services.orchestrator.memory import consolidated


class MemoryResponseTests(unittest.TestCase):
    def response(self, facts: list[dict[str, str]]) -> str:
        return json.dumps(
            {"answer": "D'après ta mémoire, le projet utilise Python.", "facts": facts}
        )

    def test_grounded_fact(self) -> None:
        answer, facts = consolidated(
            self.response(
                [{"text": "Le projet utilise Python.", "evidence": "utilise Python"}]
            ),
            "Mon projet utilise Python.",
        )
        self.assertIn("mémoire", answer)
        self.assertEqual(facts, ["Le projet utilise Python."])

    def test_ungrounded_fact_is_not_saved(self) -> None:
        with self.assertRaises(ValueError):
            consolidated(
                self.response([{"text": "Vit à Paris.", "evidence": "Paris"}]),
                "Bonjour",
            )

    def test_blank_evidence_rejected(self) -> None:
        with self.assertRaises(ValueError):
            consolidated(
                self.response([{"text": "Vit à Paris.", "evidence": ""}]), "Bonjour"
            )

    def test_invalid_transport_does_not_become_a_fact(self) -> None:
        for malformed in (
            "pas JSON",
            "{}",
            "[]",
            '{"answer":1,"facts":[]}',
            self.response([{"text": "x" * 501, "evidence": "Bonjour"}]),
        ):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                consolidated(malformed, "Bonjour")

    def test_no_fact_is_valid(self) -> None:
        answer, facts = consolidated(self.response([]), "Bonjour")
        self.assertTrue(answer)
        self.assertEqual(facts, [])
