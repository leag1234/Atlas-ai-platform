"""M10: relevant evidence must survive a large source, including Unicode."""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.orchestrator.cache import Cache
from services.orchestrator.content import select_passages, hierarchical_summary
from services.orchestrator.tools import Fetch, Runtime


class SelectionTests(unittest.TestCase):
    def test_comparison_keeps_both_separated_operation_definitions(self) -> None:
        text = "Unrelated background. " * 2000
        for identifier, fact in (("OPABC", "17 cycles"), ("OPXYZ", "29 cycles")):
            text += "\n" + identifier + "\n"
            text += "Registers transfer stored data. " * 45
            text += "\nTiming table: " + fact + ".\n"
            text += "Unrelated background. " * 2000
        _, passages, _ = hierarchical_summary(text, "Compare OPABC OPXYZ timing")
        for identifier, fact in (("OPABC", "17 cycles"), ("OPXYZ", "29 cycles")):
            self.assertTrue(
                any(identifier in p.text and fact in p.text for p in passages)
            )

    def test_technical_identifier_keeps_adjacent_operation_table(self) -> None:
        text = "General instruction timing I/O wait cycle discussion. " * 1500
        text += (
            "\nOPXYZ\nOperation description: " + "Registers transfer stored data. " * 45
        )
        text += "\nTiming table: repeating case 37 cycles; final case 29 cycles.\n"
        text += "Unrelated background. " * 1500
        summary, passages, _ = hierarchical_summary(
            text, "OPXYZ instruction timing I/O wait cycle"
        )
        self.assertIn("repeating case 37 cycles; final case 29 cycles", summary)
        self.assertTrue(
            any("OPXYZ" in p.text and "37 cycles" in p.text for p in passages)
        )

    def test_tail_evidence_and_exact_offsets(self) -> None:
        text = "Routine unrelated information. " * 5000
        text += "\nThe zephyr warranty lasts 73 months.\n"
        result = select_passages(text, "zephyr warranty", 2000)
        self.assertIn("73 months", "".join(p.text for p in result))
        self.assertLessEqual(sum(len(p.text.encode()) for p in result), 2000)
        for passage in result:
            self.assertEqual(text[passage.start : passage.end], passage.text)
        self.assertEqual([p.start for p in result], sorted(p.start for p in result))

    def test_hierarchical_document_keeps_ordered_page_coverage_and_relevance(
        self,
    ) -> None:
        pages = []
        for page in range(1, 61):
            fact = "routine status"
            if page == 30:
                fact = "MIDDLE_FACT budget is 730"
            if page == 60:
                fact = "FINAL_FACT report is postponed"
            pages.append(f"[Page {page}]\n{fact}.\n" + "background " * 20)
        text = "\n".join(pages)

        summary, passages, sections = hierarchical_summary(
            text, "summarise the beginning, middle and end", coverage=True
        )

        self.assertEqual(sections, 60)
        self.assertIn("MIDDLE_FACT budget is 730", summary)
        self.assertIn("FINAL_FACT report is postponed", summary)
        self.assertIn("[Page 1]", summary)
        self.assertIn("[Page 30]", summary)
        self.assertIn("[Page 60]", summary)
        self.assertLessEqual(len(summary.encode()), 7900)
        self.assertEqual([p.start for p in passages], sorted(p.start for p in passages))
        for passage in passages:
            self.assertEqual(text[passage.start : passage.end], passage.text)

        targeted, targeted_passages, _ = hierarchical_summary(
            text, "What does page 12 say about routine status?", coverage=True
        )
        self.assertIn("[Page 12]", targeted)
        self.assertIn("routine status", targeted)
        self.assertLessEqual(len(targeted.encode()), 7900)
        self.assertTrue(
            any("[Page 12]" in passage.text for passage in targeted_passages)
        )

    def test_unicode_and_small_budgets(self) -> None:
        text = "预算🙂 été " * 300 + "Garantie spéciale 73 mois."
        for budget in (4, 31, 700, 2000):
            result = select_passages(text, "Garantie spéciale", budget)
            self.assertTrue(result)
            self.assertLessEqual(sum(len(p.text.encode()) for p in result), budget)
            for passage in result:
                self.assertEqual(passage.text, text[passage.start : passage.end])
        with self.assertRaises(ValueError):
            select_passages(text, "q", 0)

    def test_empty_short_and_no_match(self) -> None:
        self.assertEqual(select_passages("", "q", 100), [])
        result = select_passages("short text", "q", 100)
        self.assertEqual("".join(p.text for p in result), "short text")
        self.assertEqual(result, select_passages("short text", "q", 100))
        self.assertTrue(select_passages("Other topic. " * 100, "absent", 100))

    def test_qualified_identifier_outweighs_generic_question_words(self) -> None:
        text = (
            "Documentation source return affect count cancel uncancel. " * 300
            + "\nTask.cancelling(): pending requests = cancel minus uncancel.\n"
        )
        selected = select_passages(
            text,
            "According to documentation what does Task.cancelling() return and how do cancel and uncancel affect count? Cite source.",
            800,
        )
        self.assertIn("pending requests", "".join(p.text for p in selected))

    def test_definition_after_heading_at_window_boundary(self) -> None:
        text = "cancel uncancel count return. " * 26
        text = (
            text.ljust(776)
            + "cancelling() Return pending requests: cancel less uncancel."
        )
        text += " unrelated" * 200
        selected = select_passages(
            text, "Task.cancelling() cancel uncancel count return", 1199
        )
        self.assertIn("cancel less uncancel", "".join(p.text for p in selected))

    def test_nonoverlap_determinism_and_examined(self) -> None:
        text = "Other information. " * 1000 + "Needle evidence."
        result = select_passages(text, "Needle", 2000)
        self.assertEqual(result, select_passages(text, "Needle", 2000))
        self.assertGreater(result[0].examined, len(result))
        for left, right in zip(result, result[1:]):
            self.assertLessEqual(left.end, right.start)
        self.assertEqual(select_passages("🙂", "q", 1), [])


class LargeFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_selection_depends_on_question(self) -> None:
        text = "Albatross permit: 17 days.\n" + "Unrelated content. " * 5000
        text += "\nZephyr warranty: 73 months."
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Cache(Path(directory) / "cache.sqlite"), Decimal(0))
            with (
                patch.object(
                    runtime.web,
                    "fetch",
                    new=AsyncMock(return_value=("https://example.org/final", b"html")),
                ) as fetch,
                patch("trafilatura.extract", return_value=text),
            ):
                first = await runtime.fetch(
                    Fetch(url="https://example.org", query="Zephyr warranty"), 5
                )
                second = await runtime.fetch(
                    Fetch(url="https://example.org", query="Albatross permit"), 5
                )
            self.assertIn("73 months", str(first["text"]))
            self.assertIn("17 days", str(second["text"]))
            self.assertEqual(fetch.await_count, 1)
            self.assertEqual(first["url"], "https://example.org/final")
            self.assertEqual(first["consulted_at"], second["consulted_at"])
            self.assertIn("passages", first)
            from packages.evidence import estimated_tokens

            self.assertLessEqual(estimated_tokens(str(first["text"])), 4000)
            synthesis = first["synthesis"]
            passages = first["passages"]
            assert isinstance(synthesis, dict) and isinstance(passages, list)
            self.assertEqual(synthesis["source_characters"], len(text))
            for passage in passages:
                self.assertIn(
                    text[passage["start"] : passage["end"]], str(first["text"])
                )

    async def test_original_question_used_and_failed_fetch_not_cached(self) -> None:
        text = "Unrelated filler. " * 5000 + "Zephyr warranty: 73 months."
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(
                Cache(Path(directory) / "cache.sqlite"), Decimal(0), "Zephyr warranty"
            )
            with (
                patch.object(
                    runtime.web,
                    "fetch",
                    new=AsyncMock(return_value=("https://example.org", b"html")),
                ),
                patch("trafilatura.extract", side_effect=[None, text]),
            ):
                with self.assertRaisesRegex(ValueError, "extraction_empty"):
                    await runtime.fetch(Fetch(url="https://example.org"), 5)
                output = await runtime.fetch(Fetch(url="https://example.org"), 5)
            self.assertIn("73 months", str(output["text"]))
            self.assertTrue(output["selected"])
            cached = runtime.cache.get(str(output["handle"]))
            assert cached is not None
            self.assertEqual(cached["text"], text)


class DocumentBudgetTests(unittest.TestCase):
    def test_document_budget_gives_every_page_real_content(self) -> None:
        from packages.attachment_reader import DOCUMENT_CONTEXT_BYTES
        from services.orchestrator.content import hierarchical_summary

        pages = [
            f"[Page {n}]\n" + f"Fact {n}: the committee recorded decision {n}. " * 60
            for n in range(1, 61)
        ]
        text = "\n".join(pages)
        summary, _passages, _sections = hierarchical_summary(
            text,
            "Summarise the beginning, the middle and the end.",
            budget=DOCUMENT_CONTEXT_BYTES,
            coverage=True,
        )
        self.assertLessEqual(len(summary.encode()), DOCUMENT_CONTEXT_BYTES)
        for n in (1, 30, 60):
            self.assertIn(f"Fact {n}: the committee recorded decision {n}.", summary)
        self.assertGreater(len(summary.encode()), 60 * 400)
