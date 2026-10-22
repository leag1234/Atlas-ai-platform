"""POC-E2/E3/E5: missing evidence and invented answers fail deterministically."""

import json
import unittest
from typing import Any
from unittest.mock import patch

from evals.execution import Session
from evals.suites import judged


class SuiteTests(unittest.TestCase):
    def test_citation_refusal_and_key_isolation(self) -> None:
        sent: list[dict[str, Any]] = []

        def complete(role: str, messages: list[dict[str, str]]) -> dict[str, Any]:
            sent.append({"role": role, "messages": messages})
            answer = (
                "unsupported [2]"
                if role == "system"
                else json.dumps(
                    {
                        "score": 5,
                        "supported": True,
                        "meaning_reversed": False,
                        "rationale": "test",
                    }
                )
            )
            return {"text": answer, "telemetry": {"cost": 0.001}}

        def select(suite: int) -> list[dict[str, Any]]:
            return (
                [
                    {
                        "id": f"E{suite}-test",
                        "lang": "fr",
                        "input": "question",
                        "doc_id_attendu": "SECRET_CORRECTION",
                    }
                ]
                if suite in {2, 3}
                else []
            )

        with patch("evals.suites.cases", side_effect=select):
            rows, samples = judged(
                Session(complete),
                lambda question: [{"text": "public text", "doc_id": "doc"}],
            )
        self.assertEqual([r["score"] for r in rows], [0.0, 0.0, 0.0])
        for request in sent:
            if request["role"] == "system":
                self.assertNotIn("SECRET_CORRECTION", json.dumps(request))
        self.assertEqual(len(samples), 2)

    def test_web_key_dates_are_json_serializable(self) -> None:
        from evals.suites import cases

        selected = cases(6)
        self.assertEqual(len(selected), 5)
        self.assertIn("2026-09-09", json.dumps(selected))

    def test_recorded_model_change_invalidates_cache(self) -> None:
        from evals.execution import matches_record

        messages = [{"role": "user", "content": "question"}]
        record = {"role": "system", "messages": messages, "result": {"model": "old"}}
        self.assertTrue(matches_record(record, "system", messages, "old"))
        self.assertFalse(matches_record(record, "system", messages, "changed"))
        self.assertTrue(matches_record(record, "system", messages, None))
