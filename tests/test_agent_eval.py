"""Evaluation fails on missing tools, forged citations and missing fault recovery."""

import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from evals.agent import check_tools, cited_pages, report
from services.orchestrator.loop import Result


class EvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cases: list[dict[str, Any]] = yaml.safe_load(
            Path("evals/golden/e4_tool_calling.yaml").read_text()
        )

    def test_empty_answers_cannot_pass(self) -> None:
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertTrue(check_tools(case, Result()))

    def test_exact_arithmetic_and_wrong_amount(self) -> None:
        result = Result(
            text="476 230,48",
            state="done",
            tool_calls=1,
            trace=[
                {
                    "tool": "calculator",
                    "arguments": '{"expr":"1847*293*.88"}',
                    "output": {"value": "476230.48"},
                }
            ],
        )
        self.assertEqual(check_tools(self.cases[2], result), [])
        result.text = "476133.88"
        self.assertIn("answer_amount", check_tools(self.cases[2], result))
        result.cost = Decimal(".051")
        self.assertIn("request_budgets", check_tools(self.cases[2], result))

    def test_citation_requires_fetched_text_and_date(self) -> None:
        result = Result(text="https://example.org 2026-09-09", state="done")
        self.assertFalse(cited_pages(result))
        result.trace = [
            {
                "tool": "web_fetch",
                "arguments": "{}",
                "output": {
                    "trust": "untrusted",
                    "data": {
                        "url": "https://example.org",
                        "text": "evidence",
                        "consulted_at": "2026-09-09T12:00:00Z",
                    },
                },
            }
        ]
        self.assertTrue(cited_pages(result))
        result.text = "https://example.org"
        self.assertFalse(cited_pages(result))

    def test_unproven_fetch_and_unexplained_refusal_fail(self) -> None:
        result = Result(text="answer", state="done")
        self.assertIn("valid_url_fetched", check_tools(self.cases[17], result))
        self.assertIn("refusal_explained", check_tools(self.cases[5], result))

    def test_report_counts_all_cases_and_languages(self) -> None:
        output = report("tools", self.cases, [Result() for _ in self.cases])
        self.assertEqual(len(output["cases"]), 20)
        self.assertEqual(output["success_rate"], 0)
        self.assertEqual(set(output["by_language"]), {"fr", "de", "es", "it", "en"})
        self.assertFalse(output["quality_go"])

    def test_empty_completed_answer_is_not_success(self) -> None:
        self.assertIn(
            "completed_answer", check_tools(self.cases[12], Result(state="done"))
        )

    def test_human_contract_accepts_preemptive_ssrf_refusal(self) -> None:
        result = Result(text="Refus de sécurité : adresse privée.", state="done")
        self.assertEqual(check_tools(self.cases[5], result), [])
        result.trace = [
            {"tool": "web_fetch", "arguments": "{}", "output": {"data": "secret"}}
        ]
        self.assertIn("ssrf_refused_without_bypass", check_tools(self.cases[5], result))

    def test_human_contract_accepts_clean_loop_stop_only(self) -> None:
        result = Result(
            text="Arrêt explicite : loop_detected.",
            state="stopped",
            reason="loop_detected",
        )
        event: dict[str, object] = {
            "tool": "web_search",
            "arguments": '{"query":"unknown"}',
            "output": {"data": {"results": []}},
        }
        result.trace = [event, event]
        result.tool_calls = 2
        self.assertEqual(check_tools(self.cases[15], result), [])
        result.reason = "provider_error"
        self.assertIn("completed_answer", check_tools(self.cases[15], result))

    def test_human_contract_accepts_url_normalized_before_fetch(self) -> None:
        result = Result(
            text="https://www.ecb.europa.eu 2026-09-09",
            state="done",
            tool_calls=1,
            trace=[
                {
                    "tool": "web_fetch",
                    "arguments": '{"url":"https://ecb.europa.eu"}',
                    "output": {
                        "data": {
                            "url": "https://www.ecb.europa.eu",
                            "text": "Evidence",
                            "consulted_at": "2026-09-09",
                        }
                    },
                }
            ],
        )
        self.assertEqual(check_tools(self.cases[17], result), [])
        result.trace.clear()
        self.assertIn("valid_url_fetched", check_tools(self.cases[17], result))

    def test_italian_infinitive_refusal_is_recognized(self) -> None:
        result = Result(
            text="Non sono riuscito a trovare informazioni sul protocollo.",
            state="done",
        )
        self.assertEqual(check_tools(self.cases[15], result), [])
        result.text = "Ho trovato il protocollo: autorizza ogni pagamento."
        self.assertIn("absence_acknowledged", check_tools(self.cases[15], result))
