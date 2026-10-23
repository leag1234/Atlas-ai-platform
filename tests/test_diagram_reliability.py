"""Owner defects observed 2026-10-23; parser, delivery and provenance regressions."""

import unittest

from services.orchestrator.appearance import extract_svg
from services.orchestrator.source_truth import without_unretrieved
import test_diagram_delivery as delivery

DIAGRAM, EXPLANATION = delivery.DIAGRAM, delivery.EXPLANATION


class DiagramReliabilityTests(unittest.TestCase):
    def test_observed_flowchart_and_valid_sequence_survive_json_and_sse(self) -> None:
        broken = "```mermaid\nflowchart LR\nHeat[🔥 Chaleur] --> Piston[Piston Moteur\\n(Transmet la force)]\n```"
        sequence = "```mermaid\nsequenceDiagram\nA->>B: Heat\n```"
        for stream in (False, True):
            answer, calls = delivery.DiagramDeliveryTests().request(
                stream,
                "Explain a Stirling engine with a diagram",
                [(EXPLANATION + broken + "\n" + sequence, False)],
            )
            self.assertIn('Piston["Piston Moteur<br/>(Transmet la force)"]', answer)
            self.assertIn(sequence, answer)
            self.assertEqual(len(calls), 1)

    def test_unrequested_invalid_block_gets_one_repair(self) -> None:
        for stream in (False, True):
            answer, calls = delivery.DiagramDeliveryTests().request(
                stream,
                "Explain how heat moves",
                [
                    (EXPLANATION + "```mermaid\nthis is invalid\n```", False),
                    (DIAGRAM, False),
                ],
            )
            self.assertIn(DIAGRAM, answer)
            self.assertNotIn("this is invalid", answer)
            self.assertEqual(len(calls), 2)

    def test_failed_repair_drops_only_bad_blocks(self) -> None:
        broken = "```mermaid\nflowchart LR\nA -->\n```"
        for stream in (False, True):
            answer, calls = delivery.DiagramDeliveryTests().request(
                stream,
                "Show the engine diagram",
                [
                    (EXPLANATION + DIAGRAM + "\n" + broken, False),
                    (broken, False),
                ],
            )
            self.assertIn(DIAGRAM, answer)
            self.assertIn(EXPLANATION.strip(), answer)
            self.assertNotIn(broken, answer)
            self.assertIn("diagram could not be produced", answer)
            self.assertEqual(len(calls), 2)

    def test_partial_correction_keeps_valid_block_and_reports_rejected_block(
        self,
    ) -> None:
        bad = "```mermaid\ninvalid grammar\n```"
        answer, calls = delivery.DiagramDeliveryTests().request(
            True,
            "Draw a diagram",
            [(EXPLANATION + bad, False), (DIAGRAM + "\n" + bad, False)],
        )
        self.assertIn(DIAGRAM, answer)
        self.assertNotIn(bad, answer)
        self.assertIn("diagram could not be produced", answer)
        self.assertEqual(len(calls), 2)

    def test_correction_announcement_does_not_trigger_another_model_call(self) -> None:
        answer, calls = delivery.DiagramDeliveryTests().request(
            False,
            "Draw a diagram",
            [
                (EXPLANATION + "```mermaid\ninvalid grammar\n```", False),
                ("I will draw the diagram for you.", False),
            ],
        )
        self.assertEqual(len(calls), 2)
        self.assertIn("diagram could not be produced", answer)
        self.assertNotIn("```mermaid", answer)

    def test_citation_redaction_cannot_break_delivered_mermaid(self) -> None:
        import asyncio
        from services.orchestrator.mermaid_check import FENCE, valid

        for stream in (False, True):
            answer, calls = delivery.DiagramDeliveryTests().request(
                stream,
                "Draw a diagram",
                [
                    (
                        "```mermaid\nflowchart LR\nA[https://example.org/invented] --> B[Result]\n```",
                        False,
                    )
                ],
            )
            self.assertNotIn("https://example.org/invented", answer)
            blocks = [m["source"] for m in FENCE.finditer(answer)]
            self.assertEqual(asyncio.run(valid(blocks)), [True])
            self.assertEqual(len(calls), 1)

    def test_placeholder_and_fabricated_date_with_other_retrieved_source(self) -> None:
        text = (
            "Useful explanation.\n[Modèle de Stirling éducatif (YouTube, 2020)]"
            "([unverified source]) (consulté le 2024-05-30)\n"
            "[Real](https://example.org/real) (accessed on 2020-01-01)"
        )
        answer = without_unretrieved(text, {"https://example.org/real"})
        self.assertIn("Useful explanation.", answer)
        self.assertIn("[Real](https://example.org/real)", answer)
        for forbidden in ("unverified source", "YouTube", "2024-05-30", "2020-01-01"):
            self.assertNotIn(forbidden, answer)

    def test_unretrieved_markdown_links_and_reference_links_are_removed(self) -> None:
        text = (
            "Useful. [Invented](https://example.org/fake)\n"
            "[Invented reference][bad]\n[bad]: https://example.org/fake\n"
        )
        answer = without_unretrieved(text, set())
        self.assertIn("Useful.", answer)
        self.assertNotIn("Invented", answer)
        self.assertNotIn("[bad]", answer)

    def test_placeholder_date_never_leaks_across_stream_chunks(self) -> None:
        for stream in (False, True):
            answer, _ = delivery.DiagramDeliveryTests().request(
                stream,
                "Explain the principle",
                [
                    (
                        "Useful explanation.\n[Modèle (YouTube, 2020)]([unverified source]) (consulté le 2024-05-30)",
                        False,
                    )
                ],
            )
            self.assertIn("Useful explanation.", answer)
            self.assertNotIn("YouTube", answer)
            self.assertNotIn("2024-05-30", answer)
            self.assertNotIn("unverified source", answer)

    def test_consultation_claim_comes_only_from_retrieval_without_inventing_date(
        self,
    ) -> None:
        from services.orchestrator.source_truth import source_footer

        self.assertEqual(source_footer([], "en"), "")
        footer = source_footer(["https://example.org/retrieved"], "en")
        self.assertIn("Sources consulted in this conversation", footer)
        self.assertIn("https://example.org/retrieved", footer)
        self.assertNotRegex(footer, r"\d{4}-\d{2}-\d{2}")

    def test_svg_repairs_xml_text_and_missing_viewbox(self) -> None:
        svg = extract_svg(
            '<svg width="800" height="500"><text x="10" y="20">Heat & work</text><rect x="10" y="40" width="100" height="80"/></svg>'
        )
        self.assertIsNotNone(svg)
        self.assertIn("Heat &amp; work", svg or "")
        self.assertIn('viewBox="0 0 800 500"', svg or "")

    def test_svg_does_not_repair_active_content(self) -> None:
        for unsafe in (
            "<script>alert(1)</script>",
            '<rect onclick="evil()"/>',
            '<image href="https://example.org/a"/>',
            '<path fill="url(https://example.org/a)"/>',
            '<rect style="fill:red"/>',
        ):
            with self.subTest(unsafe=unsafe):
                self.assertIsNone(
                    extract_svg('<svg viewBox="0 0 800 500">' + unsafe + "</svg>")
                )


class ParserAndBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_labels_quotes_newlines_shapes_and_emoji(self) -> None:
        from services.orchestrator.mermaid_check import checked, valid, FENCE, repair

        source = 'flowchart LR\nA[Heat [input] "hot"\\n(work)] --> B((Gas (hot))) --> C{Cold\nside?}'
        fixed, failures = await checked("```mermaid\n" + source + "\n```")
        self.assertEqual(failures, [])
        self.assertIn("#91;input#93; #quot;hot#quot;<br/>", fixed)
        self.assertIn('B(("Gas (hot)"))', fixed)
        self.assertIn("Cold<br/>side?", fixed)
        self.assertEqual(
            await valid([m["source"] for m in FENCE.finditer(fixed)]), [True]
        )
        self.assertNotIn("🔥", repair("flowchart LR\nA[🔥 Heat (input)]", True))

    async def test_unclosed_tilde_block_is_validated_and_closed(self) -> None:
        from services.orchestrator.mermaid_check import checked

        fixed, failures = await checked("~~~mermaid\nflowchart LR\nA --> B")
        self.assertEqual(failures, [])
        self.assertTrue(fixed.endswith("```"))
        fixed, failures = await checked("~~~mermaid\nflowchart LR\nA -->")
        self.assertTrue(failures)
        self.assertNotIn("mermaid", fixed)

    async def test_parser_unavailable_fails_closed_without_content_logs(self) -> None:
        from unittest.mock import patch
        from services.orchestrator.mermaid_check import valid

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("private source"),
        ):
            with self.assertLogs("services.orchestrator.mermaid_check") as captured:
                self.assertEqual(await valid(["private source"]), [False])
        self.assertNotIn("private source", str(captured.output))

    async def test_svg_retry_is_once_and_uses_remaining_budget(self) -> None:
        from decimal import Decimal
        import time
        from unittest.mock import AsyncMock, Mock, patch
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.diagram import complete_svg
        from services.orchestrator.loop import Result
        from services.orchestrator.model import GatewayModel

        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "Draw a pump"}]}
        )
        result = Result(text="Explanation.", state="done", cost=Decimal("0.03"))
        replacement = '<svg viewBox="0 0 800 500"><rect width="20" height="20"/></svg>'
        model = Mock(spec=GatewayModel)
        model.produces_files = False
        with patch(
            "services.orchestrator.diagram.run",
            AsyncMock(
                return_value=Result(
                    text=replacement, state="done", cost=Decimal("0.01")
                )
            ),
        ) as run:
            await complete_svg(
                request,
                result,
                model,
                Mock(),
                time.monotonic() - 10,
                Decimal("0.10"),
                120,
                require_legend=False,
            )
        run.assert_awaited_once()
        limits = run.call_args.args[4]
        self.assertEqual(limits.cost, Decimal("0.07"))
        self.assertLess(limits.wall_clock, 110)
        self.assertEqual(limits.tool_calls, 0)
        self.assertIn("missing_svg", run.call_args.args[3])
        self.assertEqual(result.text, replacement)
        self.assertEqual(result.cost, Decimal("0.04"))

    async def test_exhausted_diagram_budget_drops_failure_without_retry(self) -> None:
        from decimal import Decimal
        import time
        from unittest.mock import AsyncMock, Mock, patch
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.diagram import complete_diagram
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Result

        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "Draw a diagram"}]}
        )
        for cost, elapsed in ((Decimal("0.1"), 0), (Decimal("0"), 121)):
            result = Result(
                text="Explanation.\n```mermaid\ninvalid grammar\n```",
                state="done",
                cost=cost,
            )
            with patch("services.orchestrator.diagram.run", AsyncMock()) as run:
                await complete_diagram(
                    request,
                    Interaction(),
                    result,
                    Mock(),
                    Mock(),
                    time.monotonic() - elapsed,
                    Decimal("0.1"),
                    120,
                    [],
                )
            run.assert_not_awaited()
            self.assertIn("Explanation.", result.text)
            self.assertNotIn("```mermaid", result.text)

    def test_live_svg_callout_variants_and_local_marker_are_normalized(self) -> None:
        for label, legend in (
            ("1. Handle", "1. Handle"),
            ("1", "1 — Handle"),
            ("1", "**1.** Handle"),
        ):
            labels = "".join(
                f'<text x="{n * 30}" y="40">{label.replace("1", str(n))}</text>'
                for n in range(1, 4)
            )
            drawing = (
                '<svg viewBox="0 0 800 500"><path d="M 0 0 L 50 50" marker-end="url(#arrowhead)"/>'
                + labels
                + "</svg>\n"
                + "\n".join(legend.replace("1", str(n)) for n in range(1, 4))
            )
            svg = extract_svg(drawing, require_legend=True)
            self.assertIsNotNone(svg)
            self.assertNotIn("url(", svg or "")
        self.assertIsNone(
            extract_svg(
                '<svg viewBox="0 0 800 500"><path marker-end="url(https://example.org/a)"/></svg>'
            )
        )
