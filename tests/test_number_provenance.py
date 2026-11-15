"""Offline checks for number provenance (stage 0, log only) with fictional data."""

from decimal import Decimal
import json
import os
import time
import unittest
from unittest.mock import patch

from services.orchestrator import number_provenance
from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.number_provenance import (
    Provenance,
    candidates,
    number,
)


def report(answer: str, evidence: object = None, said: str = "") -> dict[str, int]:
    tracker = Provenance()
    if evidence is not None:
        tracker.collect(evidence)
    return tracker.report(answer, [said] if said else []).fields()


class NumberTests(unittest.TestCase):
    def test_reads_english_and_european_formats(self) -> None:
        self.assertEqual(number("1,234.5"), Decimal("1234.5"))
        self.assertEqual(number("1 234,5"), Decimal("1234.5"))
        self.assertEqual(number("1 234"), Decimal("1234"))
        self.assertEqual(number("1.234.567,25"), Decimal("1234567.25"))
        self.assertEqual(number("2,5"), Decimal("2.5"))


class CandidateTests(unittest.TestCase):
    def test_correct_written_calculation_is_recomputed(self) -> None:
        fields = report("The sum is 1200 + 1350 + 980 = 3530.")
        self.assertEqual(fields["computed"], 1)
        self.assertEqual(fields["recomputed"], 1)
        self.assertEqual(fields["wrong"], 0)

    def test_wrong_written_calculation_is_counted(self) -> None:
        fields = report("Budget left: 150 - 66 = 74 units.")
        self.assertEqual(fields["wrong"], 1)

    def test_chained_and_negative_calculations(self) -> None:
        fields = report(
            "Variance: 58 / (8 - 1) = 58 / 7 ≈ 8.29. Change: 980 - 1350 = -370."
        )
        self.assertEqual(fields["recomputed"], 2)
        self.assertEqual(fields["wrong"], 0)

    def test_total_from_a_tool_result_is_supported(self) -> None:
        fields = report("The total is 4 321 units.", {"value": "4321"})
        self.assertEqual(fields["computed"], 1)
        self.assertEqual(fields["evidence"], 1)

    def test_rounded_and_percentage_forms_of_evidence_are_supported(self) -> None:
        evidence = {"data": {"text": "ratio 0.125, mean 8.285714"}}
        self.assertEqual(report("soit 12,5 % du total.", evidence)["evidence"], 1)
        self.assertEqual(report("The average is 8.29.", evidence)["evidence"], 1)
        self.assertEqual(report("The average is about 8.3.", evidence)["evidence"], 1)

    def test_number_restated_from_the_conversation(self) -> None:
        fields = report(
            "In total you listed 17 boxes.", said="I have 17 boxes in the shed"
        )
        self.assertEqual(fields["conversation"], 1)
        self.assertEqual(fields["unsupported"], 0)

    def test_unit_conversions_are_counted_apart(self) -> None:
        fields = report("For reference: 1 cup = 8 fl oz and 1/2 cup = 4 fl oz.")
        self.assertEqual(fields["conversion"], 2)
        self.assertEqual(fields["wrong"] + fields["unsupported"], 0)

    def test_signed_difference_is_recomputed(self) -> None:
        self.assertEqual(report("Change: 1350 - 1200 = +150.")["recomputed"], 1)

    def test_unsupported_computed_number(self) -> None:
        fields = report("The average monthly spend is 57.3 units.", {"text": "none"})
        self.assertEqual(fields["unsupported"], 1)

    def test_plain_facts_list_numbers_years_and_code_are_ignored(self) -> None:
        answer = (
            "1. Total cost overview\n"
            "In total, 2024 was a quiet year for Valmorel.\n"
            "The bridge has 12 arches.\n"
            "```\ntotal = 99\n```\n"
            "<details><summary>Calculating…</summary>average 41</details>\n"
            "See https://example.test/total=77 for details."
        )
        self.assertEqual(candidates(answer), [])

    def test_tool_errors_and_links_are_not_evidence(self) -> None:
        tracker = Provenance()
        tracker.collect({"error": "invalid_expression", "value": "88"})
        tracker.collect(
            {"results": [{"link": "https://example.test/88", "snippet": "x"}]}
        )
        self.assertNotIn(Decimal(88), tracker.values)


def bank_sum(terms: int, result: bool = False) -> str:
    """A long French sum like a bank export total, with or without its result."""
    amounts = [
        Decimal(f"{(i * 37) % 90 + 10}.{(i * 13) % 90 + 10}") for i in range(terms)
    ]
    written = " + ".join(format(amount, "f").replace(".", ",") for amount in amounts)
    total = format(sum(amounts), "f").replace(".", ",")
    return f"Total : {written}{' = ' + total if result else ''} euros.\n"


def elapsed(action: object) -> float:
    start = time.perf_counter()
    action()  # type: ignore[operator]
    return time.perf_counter() - start


class ScanCostTests(unittest.TestCase):
    """The scan is linear: a long answer never holds the interpreter (staging 2026-11-15)."""

    def test_long_sums_without_result_finish_fast(self) -> None:
        for terms in (20, 50, 200):
            with self.subTest(terms=terms):
                answer = bank_sum(terms)
                self.assertLess(elapsed(lambda: report(answer)), 0.05)

    def test_long_sum_with_its_result_is_still_recomputed(self) -> None:
        fields = report(bank_sum(20, result=True))
        self.assertEqual((fields["computed"], fields["recomputed"]), (1, 1))

    def test_overlong_calculation_and_line_are_skipped(self) -> None:
        self.assertEqual(report(bank_sum(80, result=True))["computed"], 0)
        line = "au total 4321 " + "word " * (number_provenance.MAX_LINE // 5)
        self.assertEqual(candidates(line), [])
        self.assertEqual(len(candidates("au total 4321\n" + line)), 1)

    def test_pathological_answers_finish_fast(self) -> None:
        answers = [
            "1 + " * 20_000,
            "(1 + " * 20_000,
            "1 " * 50_000,
            "= 1 + " * 20_000,
            "au total 12 " * 10_000,
            " " * 100_000 + "x",
            "[a](" * 25_000,
            "<details>" * 10_000,
        ]
        for answer in answers:
            with self.subTest(answer=answer[:12]):
                self.assertLess(elapsed(lambda: report(answer)), 1.0)

    def test_large_evidence_pool_matches_a_full_scan(self) -> None:
        tracker = Provenance()
        tracker.add_text(" ".join(f"{i * 7.31:.2f}" for i in range(40_000)))
        answer = " ".join(f"Total {i * 3.7:.1f} units." for i in range(1, 301))
        start = time.perf_counter()
        fields = tracker.report(answer, []).fields()
        self.assertLess(time.perf_counter() - start, 1.0)
        # The former full scan, on a sample: same verdict for every candidate.
        pool = number_provenance.Pool(tracker.values)
        for candidate in candidates(answer)[:40]:
            decimals = number_provenance.places(candidate.token)
            tolerance = Decimal("0.01") if candidate.approximate else Decimal(0)
            full = any(
                number_provenance.matches(candidate.value, decimals, value, tolerance)
                or number_provenance.matches(
                    candidate.value, decimals, value * 100, tolerance
                )
                for value in tracker.values
            )
            self.assertEqual(tracker.supported(candidate, pool), full)
        self.assertEqual(fields["computed"], 300)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def run_turn(self, flag: str) -> tuple[Interaction, list[str]]:
        answer = "The sum is 1200 + 1350 = 2550. The total is 3530 units."

        async def generate(request: ChatRequest, item: Interaction) -> None:
            number_provenance.observe({"value": "3530"})
            item.reponse = answer
            item.state = "done"

        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "Add 1200 and 1350"}]}
        )
        item = Interaction()
        with (
            patch.dict(os.environ, {"ATLAS_NUMBER_PROVENANCE": flag}),
            patch(
                "services.orchestrator.chat_pipeline._process_request",
                side_effect=generate,
            ),
            self.assertLogs(number_provenance.LOGGER, "INFO") as logs,
        ):
            number_provenance.LOGGER.info("start")
            await process(request, item)
        self.assertEqual(item.reponse, answer)
        return item, [line for line in logs.output if "number_provenance " in line]

    async def test_log_mode_reports_counts_without_changing_the_answer(self) -> None:
        _, lines = await self.run_turn("log")
        self.assertEqual(len(lines), 1)
        fields = json.loads(lines[0].split("number_provenance ", 1)[1])
        self.assertEqual(fields["computed"], 2)
        self.assertEqual(fields["recomputed"], 1)
        self.assertEqual(fields["evidence"], 1)
        self.assertEqual(fields["tool_results"], 1)
        self.assertNotIn("3530", lines[0])

    async def test_off_by_default(self) -> None:
        _, lines = await self.run_turn("off")
        self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
