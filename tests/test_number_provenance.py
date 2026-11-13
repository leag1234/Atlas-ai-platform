"""Offline checks for number provenance (stage 0, log only) with fictional data."""

from decimal import Decimal
import json
import os
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
