"""Offline checks for precise-claim grounding with fictional data."""

import unittest
from unittest.mock import patch

from services.orchestrator import grounding
from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.grounding import (
    WORDING,
    Grounding,
    extract_claims,
    factual_query,
    needs_grounding,
)
from services.orchestrator.interactions import Interaction
from services.orchestrator.stream_client import sink_context


class GroundingTests(unittest.TestCase):
    def test_extracts_dates_numbers_and_proper_names(self) -> None:
        claims = extract_claims(
            "Northbridge opened in 1924. It cost 12 million units. "
            'The novel "Winter Glass" was written by Aster Bell.'
        )
        self.assertEqual(len(claims), 3)
        self.assertIn("1924", claims[0].values)
        self.assertIn("12 million", claims[1].values)
        self.assertIn("winter glass", claims[2].values)
        self.assertIn("aster", claims[2].values)
        self.assertIn("bell", claims[2].values)

    def test_supported_claim_is_unchanged(self) -> None:
        checker = Grounding()
        checker.add("Northbridge opened in 1924 and cost 12 million units.")
        answer = "Northbridge opened in 1924 and cost 12 million units."
        self.assertEqual(checker.check(answer, "Opening?", "en"), answer)
        self.assertEqual(checker.claims_checked, 1)
        self.assertEqual(checker.claims_changed, 0)

    def test_unsupported_claim_without_evidence_is_dropped(self) -> None:
        checker = Grounding()
        answer = checker.check("Northbridge opened in 1924.", "Opening?", "en")
        self.assertNotIn("Northbridge opened in 1924", answer)
        self.assertIn("cannot verify", answer)
        self.assertEqual(checker.claims_changed, 1)

    def test_unsupported_claim_with_evidence_is_marked_not_quoted_as_fact(self) -> None:
        checker = Grounding()
        checker.add("Northbridge opened in 1924.")
        answer = checker.check("Northbridge opened in 1925.", "Opening?", "en")
        self.assertIn("Unverified claim", answer)
        self.assertIn("> Northbridge opened in 1925.", answer)

    def test_no_sources_and_no_precise_claims_are_unchanged(self) -> None:
        checker = Grounding()
        answer = "The two fictional places are described differently."
        self.assertEqual(checker.check(answer, "Compare them.", "en"), answer)
        self.assertEqual(checker.claims_checked, 0)

    def test_code_blocks_are_unchanged(self) -> None:
        checker = Grounding()
        answer = "Here is the function:\n```python\nvalue = 1924\n```"
        self.assertEqual(checker.check(answer, "Write a function.", "en"), answer)
        self.assertEqual(checker.claims_checked, 0)

    def test_false_premise_is_corrected_from_retrieved_source(self) -> None:
        checker = Grounding()
        checker.add("Northbridge opened in 1924.")
        answer = checker.check(
            "Northbridge opened in 1924, not 1912.",
            "Why was Northbridge opened in 1912?",
            "en",
        )
        self.assertIn("1924, not 1912", answer)
        self.assertIn("> Northbridge opened in 1924.", answer)
        self.assertEqual(checker.premises_checked, 1)

    def test_premise_search_is_neutral(self) -> None:
        self.assertEqual(
            factual_query("Why was Northbridge opened in 1912?"),
            "was Northbridge opened in ?",
        )

    def test_creative_request_does_not_trigger_search(self) -> None:
        self.assertEqual(factual_query('Translate "Winter Glass"'), "")

    def test_wording_exists_in_every_ui_language(self) -> None:
        for key, table in WORDING.items():
            self.assertEqual(set(table), {"en", "fr", "de", "es", "it"}, key)

    def test_unsupported_claim_uses_the_answer_language(self) -> None:
        answer = "Northbridge opened in 1924."
        for language in ("en", "fr", "de", "es", "it"):
            checked = Grounding().check(answer, "When did Northbridge open?", language)
            self.assertIn(WORDING["missing"][language], checked)
            self.assertNotIn("1924", checked)
        checked = Grounding().check(answer, "When did Northbridge open?", "pt")
        self.assertIn(WORDING["missing"]["en"], checked)


class FactualQuestionTests(unittest.TestCase):
    def test_precise_fact_questions_in_every_ui_language(self) -> None:
        for question in (
            "When did Northbridge open?",
            "Quand le pont de Valmorel a-t-il ouvert ?",
            "Wann wurde die Brücke von Valmorel eröffnet?",
            "¿Cuándo se inauguró el puente de Valmorel?",
            "Quando è stato aperto il ponte di Valmorel?",
            "How many people live in Valmorel?",
            "What happened in 1924 in Valmorel?",
        ):
            self.assertTrue(needs_grounding(question), question)

    def test_creative_conversational_and_open_questions_keep_streaming(self) -> None:
        for question in (
            "Write a poem about the sea",
            "bonjour",
            "Explain recursion simply",
            "Peux-tu m'aider à rédiger un courriel poli ?",
        ):
            self.assertFalse(needs_grounding(question), question)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def run_turn(
        self, question: str, answer: str, evidence: str | None
    ) -> tuple[Interaction, list[dict[str, object]]]:
        events: list[dict[str, object]] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        async def generate(request: ChatRequest, item: Interaction) -> None:
            upstream = sink_context.get()
            assert upstream is not None
            if evidence is not None:
                await upstream({"phase": "tool_started", "tool": "web_search"})
                check = grounding.current.get()
                assert check is not None
                check.collect({"results": [{"title": "Valmorel", "snippet": evidence}]})
            await upstream({"delta": {"content": answer}})
            item.reponse = answer
            item.state = "done"

        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": question}]}
        )
        item = Interaction()
        token = sink_context.set(sink)
        try:
            with patch(
                "services.orchestrator.chat_pipeline._process_request",
                side_effect=generate,
            ):
                await process(request, item)
        finally:
            sink_context.reset(token)
        return item, events

    async def test_factual_turn_never_streams_an_unsupported_claim(self) -> None:
        item, events = await self.run_turn(
            "When did the Valmorel bridge open?",
            "The Valmorel bridge opened in 1931.",
            "The Valmorel bridge opened to traffic in 1927.",
        )
        self.assertNotIn("1931", str(events))
        self.assertNotIn("1931", item.reponse.split(">")[0])
        self.assertTrue(any(event.get("phase") == "tool_started" for event in events))

    async def test_factual_turn_keeps_a_supported_answer(self) -> None:
        item, events = await self.run_turn(
            "When did the Valmorel bridge open?",
            "The Valmorel bridge opened in 1927.",
            "The Valmorel bridge opened to traffic in 1927.",
        )
        self.assertEqual(item.reponse, "The Valmorel bridge opened in 1927.")
        self.assertFalse(any("delta" in event for event in events))

    async def test_factual_turn_without_sources_is_unchanged(self) -> None:
        item, _ = await self.run_turn(
            "When did the Valmorel bridge open?",
            "The Valmorel bridge opened in 1927.",
            None,
        )
        self.assertEqual(item.reponse, "The Valmorel bridge opened in 1927.")

    async def test_other_turns_keep_streaming(self) -> None:
        item, events = await self.run_turn(
            "Explain recursion simply", "Recursion is a function calling itself.", None
        )
        self.assertIn({"delta": {"content": item.reponse}}, events)


if __name__ == "__main__":
    unittest.main()
