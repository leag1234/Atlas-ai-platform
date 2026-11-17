"""Answers and files derived from a provided source are checked against it.

Self-written texts with fictional names; no model is called (the judge and the
rewrite are recorded replies).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import os
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator import document_quotes
from services.orchestrator import source_faithfulness as faith
from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.model import GatewayError
from services.orchestrator.stream_client import sink_context

SOURCE = (
    "The Varnholt footbridge crosses the river Ilse in the town of Brenzac. "
    "Work started in March 2019 and the bridge opened on 14 October 2020. "
    "The deck is 212 metres long and rests on two concrete piers. "
    "The town council chose a steel deck because it weighed less than a "
    "concrete one. A flood in May 2020 stopped the work for six weeks. "
    "The bridge cost 12.4 million euros, paid by the town and the region. "
    "Engineers from the firm Quedrin Halsted designed the cables. "
    "Since its opening, about 3 000 people cross it every day. "
) * 2
FAITHFUL = (
    "The Varnholt footbridge over the Ilse in Brenzac opened on 14 October 2020, "
    "after work that started in March 2019. A flood in May 2020 stopped the "
    "work for six weeks. Its 212-metre steel deck was chosen because it weighed "
    "less than concrete."
)
# One link the source never states: the flood is not given as the cause.
CAUSAL = (
    "The Varnholt footbridge over the Ilse in Brenzac opened on 14 October 2020. "
    "Because of the flood in May 2020, the town council chose a steel deck. "
    "About 3 000 people cross it every day."
)
FLAGGED = "Because of the flood in May 2020, the town council chose a steel deck."
QUESTION = "Summarize this document for me please."


def source() -> faith.Source:
    sources = document_quotes.Sources()
    sources.add_document(SOURCE, "bridge.txt")
    sources.add_conversation(QUESTION)
    found = faith.provided(sources)
    assert found is not None
    return found


def judged(
    unsupported: list[dict[str, str]], links: list[dict[str, Any]] = []
) -> dict[str, str]:
    return {
        "text": json.dumps({"links": links, "unsupported": unsupported}),
        "cost_eur": "0.006",
    }


CLEAN = judged(
    [], [{"text": "because it weighed less than concrete", "source_states_it": True}]
)
CAUGHT = judged(
    [{"text": FLAGGED, "reason": "the source does not link the flood to the choice"}],
    [{"text": "Because of the flood in May 2020", "source_states_it": False}],
)


class Calls:
    """Recorded replies in order; every payload is kept."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.payloads: list[dict[str, Any]] = []

    async def __call__(self, payload: dict[str, Any], seconds: float) -> dict[str, Any]:
        self.payloads.append(payload)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return dict(reply)


def review(answer: str, calls: Calls, **options: Any) -> tuple[str, str, faith.Report]:
    given = options.pop("source", None) or source()
    arguments: dict[str, Any] = {
        "request": QUESTION,
        "profile": "atlas-qwen",
        "language": "en",
        "streamed": False,
        "complete": calls,
    }
    arguments.update(options)
    return asyncio.run(faith.review(answer, given, **arguments))


class SourceTests(unittest.TestCase):
    def test_no_document_and_no_long_paste_is_no_source(self) -> None:
        sources = document_quotes.Sources()
        sources.add_conversation("What is a footbridge?")
        self.assertIsNone(faith.provided(sources))
        self.assertIsNone(faith.provided(None))

    def test_passages_a_search_retrieved_are_not_a_provided_source(self) -> None:
        # A corpus search turn quotes its passages (document_quotes) but did not
        # restate a document the person gave: it is not checked here.
        sources = document_quotes.Sources()
        sources.add_document(SOURCE, retrieved=True)
        sources.add_conversation(QUESTION)
        self.assertTrue(sources.active())
        self.assertIsNone(faith.provided(sources))
        sources.add_document("The deck is made of oak.", "deck.txt")
        found = faith.provided(sources)
        assert found is not None
        self.assertIn("[Document 1: deck.txt]", found.text)
        self.assertNotIn(SOURCE, found.text)

    def test_a_pasted_text_is_a_source_and_short_messages_are_the_person_own(
        self,
    ) -> None:
        sources = document_quotes.Sources()
        sources.add_conversation(SOURCE)
        sources.add_conversation("Make it shorter, in French.")
        found = faith.provided(sources)
        assert found is not None
        self.assertIn("[Text the person pasted in the conversation]", found.text)
        self.assertEqual(found.passages, [SOURCE])
        self.assertEqual(found.own, ["Make it shorter, in French."])

    def test_a_document_is_named_and_tool_results_join_the_source(self) -> None:
        sources = document_quotes.Sources()
        sources.add_document(SOURCE, "bridge.txt")
        sources.collect({"result": "The region paid 40 percent."})
        found = faith.provided(sources)
        assert found is not None
        self.assertIn("[Document 1: bridge.txt]", found.text)
        self.assertIn("The region paid 40 percent.", found.text)


class LocateTests(unittest.TestCase):
    body = (
        "The bridge opened in 2020. It cost 12.4 million euros, paid by the town.\n"
        "- Engineers from **Quedrin Halsted** designed the cables."
    )

    def place(self, quote: str) -> tuple[str, bool] | None:
        found = faith.locate(quote, self.body, faith.sentences(self.body))
        return (
            None
            if found is None
            else (self.body[found[0] : found[1]].strip(), found[2])
        )

    def test_verbatim_with_formatting_and_case_removed(self) -> None:
        self.assertEqual(
            self.place("engineers from Quedrin Halsted designed"),
            ("- Engineers from **Quedrin Halsted** designed the cables.", True),
        )

    def test_gap_markers_and_left_out_words_keep_the_words_in_order(self) -> None:
        located = self.place("It cost ... paid by the town")
        self.assertEqual(
            located, ("It cost 12.4 million euros, paid by the town.", True)
        )
        self.assertEqual(self.place("It cost euros, paid by the town")[1], True)  # type: ignore[index]
        # A remark the judge put in brackets is its own.
        self.assertEqual(
            self.place("opened in 2020 (after the flood)"),
            ("The bridge opened in 2020.", True),
        )

    def test_a_paraphrase_is_located_by_overlap_and_marked_not_verbatim(self) -> None:
        self.assertEqual(
            self.place("the cables were designed by engineers from Quedrin Halsted"),
            ("- Engineers from **Quedrin Halsted** designed the cables.", False),
        )

    def test_words_the_answer_does_not_hold_are_not_located(self) -> None:
        self.assertIsNone(self.place("The mayor cut the ribbon in front of the crowd"))

    def test_a_script_without_spaces_is_matched_by_its_characters(self) -> None:
        body = "橋は約120億円をかけて2020年に完成した。地震で損傷はなかった。"
        found = faith.locate("約120億円をかけて完成した", body, faith.sentences(body))
        assert found is not None
        self.assertEqual(
            body[found[0] : found[1]], "橋は約120億円をかけて2020年に完成した。"
        )
        self.assertTrue(found[2])


class VerdictTests(unittest.TestCase):
    def test_unsupported_statements_and_unstated_links_become_findings(self) -> None:
        verdict = faith.read_verdict(str(CAUGHT["text"]), CAUSAL)
        assert verdict is not None
        # The statement and its link are the same sentence: one finding.
        self.assertEqual(len(verdict.findings), 1)
        self.assertEqual(
            CAUSAL[verdict.findings[0].start : verdict.findings[0].end].strip(), FLAGGED
        )
        self.assertEqual(verdict.links, 1)

    def test_a_stated_link_quoted_verbatim_is_no_finding(self) -> None:
        verdict = faith.read_verdict(str(CLEAN["text"]), FAITHFUL)
        assert verdict is not None
        self.assertEqual(verdict.findings, [])
        self.assertEqual(verdict.not_confirmed, 0)

    def test_a_stated_link_the_answer_does_not_hold_word_for_word_is_not_confirmed(
        self,
    ) -> None:
        reply = json.dumps(
            {
                "links": [
                    {
                        "text": "concrete was rejected since the steel deck weighed less",
                        "source_states_it": True,
                    }
                ],
                "unsupported": [],
            }
        )
        verdict = faith.read_verdict(reply, FAITHFUL)
        assert verdict is not None
        self.assertEqual(verdict.not_confirmed, 1)
        self.assertEqual([f.kind for f in verdict.findings], ["not_confirmed"])

    def test_a_quotation_found_nowhere_in_the_answer_is_dropped_and_counted(
        self,
    ) -> None:
        reply = json.dumps(
            {
                "links": [],
                "unsupported": [
                    {
                        "text": "The mayor resigned after the opening ceremony",
                        "reason": "x",
                    }
                ],
            }
        )
        verdict = faith.read_verdict(reply, FAITHFUL)
        assert verdict is not None
        self.assertEqual(verdict.findings, [])
        self.assertEqual(verdict.unlocated, 1)

    def test_an_unreadable_reply_is_no_verdict(self) -> None:
        self.assertIsNone(faith.read_verdict("I think it is fine.", FAITHFUL))
        self.assertIsNone(faith.read_verdict('{"links": []}', FAITHFUL))


class HintTests(unittest.TestCase):
    def test_numbers_absent_from_the_source_are_found(self) -> None:
        answer = (
            "The bridge opened on 14 October 2021 and cost 15 million euros; "
            "its A380-sized piers are 40% taller."
        )
        self.assertEqual(faith.hints(answer, source()), ["2021", "15 million", "40%"])

    def test_numbers_in_another_written_form_are_not_hints(self) -> None:
        answer = (
            "Le pont a ouvert le 14 octobre 2020 ; il a coûté 12,4 millions "
            "d'euros. Environ 3000 Personen nutzen ihn täglich, 212 m."
        )
        self.assertEqual(faith.hints(answer, source()), [])

    def test_the_judge_reads_the_request_the_source_and_the_answer(self) -> None:
        content = faith.judge_messages(QUESTION, "SRC", "ANSWER")[0]["content"]
        self.assertTrue(content.startswith(faith.JUDGE))
        self.assertTrue(
            content.endswith(
                "REQUEST:\n" + QUESTION + "\n\nSOURCE:\nSRC\n\nSUMMARY:\nANSWER"
            )
        )


# The judge alone: the fast screen in front of it has its own tests (ScreenTests).
WITHOUT_SCREEN = patch.object(faith, "screen_profile", return_value=None)


@WITHOUT_SCREEN
class ReviewTests(unittest.TestCase):
    def test_a_faithful_answer_is_unchanged_after_one_call(self) -> None:
        calls = Calls(CLEAN)
        text, addition, report = review(FAITHFUL, calls)
        self.assertEqual((text, addition), (FAITHFUL, ""))
        self.assertEqual(report.outcome, "clean")
        self.assertEqual(report.cost_eur, Decimal("0.006"))
        payload = calls.payloads[0]
        self.assertEqual(payload["profile"], "atlas-qwen")
        self.assertEqual(payload["tools"], [])
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["max_tokens"], faith.JUDGE_TOKENS)
        self.assertIn(SOURCE, payload["messages"][0]["content"])

    def test_the_judge_runs_on_the_profile_of_its_role(self) -> None:
        calls = Calls(CLEAN)
        roles = {
            "source_faithfulness": "atlas-judge",
            "source_faithfulness_rewrite": "atlas-fast",
        }
        with patch("packages.profiles.picture_profile", side_effect=roles.get):
            review(FAITHFUL, calls)
        self.assertEqual(calls.payloads[0]["profile"], "atlas-judge")

    def test_a_flagged_sentence_is_rewritten_once_and_checked_again(self) -> None:
        fixed = (
            "The town council chose a steel deck because it weighed less than concrete."
        )
        calls = Calls(
            CAUGHT, {"text": json.dumps({"1": fixed}), "cost_eur": "0.001"}, CLEAN
        )
        text, addition, report = review(CAUSAL, calls)
        self.assertEqual(text, CAUSAL.replace(FLAGGED, fixed))
        self.assertEqual(addition, "")
        self.assertEqual(report.outcome, "revised")
        self.assertEqual(report.rewrite, "kept")
        self.assertEqual(report.calls, 3)
        self.assertEqual(report.cost_eur, Decimal("0.013"))
        rewrite = calls.payloads[1]
        self.assertEqual(rewrite["max_tokens"], faith.REWRITE_TOKENS)
        sentences = rewrite["messages"][1]["content"].split("SOURCE:")[0]
        self.assertIn("[1] " + FLAGGED, sentences)
        self.assertNotIn("[2]", sentences)
        self.assertNotIn("3 000 people", sentences)
        # The rewrite is judged as an answer of its own.
        self.assertIn(fixed, calls.payloads[2]["messages"][0]["content"])

    def test_what_the_rewrite_leaves_unsupported_is_noted_in_the_answer_language(
        self,
    ) -> None:
        calls = Calls(
            CAUGHT, {"text": json.dumps({"1": FLAGGED}), "cost_eur": "0"}, CAUGHT
        )
        text, addition, report = review(CAUSAL, calls, language="fr")
        self.assertEqual(text, CAUSAL)
        self.assertTrue(addition.startswith("*Vérifié sur le document fourni"))
        self.assertIn("« " + FLAGGED + " »", addition)
        self.assertEqual(report.outcome, "noted")
        self.assertEqual(report.findings_after, 1)

    def test_a_streamed_answer_is_not_rewritten_and_gets_the_note(self) -> None:
        calls = Calls(CAUGHT)
        text, addition, report = review(CAUSAL, calls, streamed=True)
        self.assertEqual(text, CAUSAL)
        self.assertEqual(
            addition,
            "*Checked against the provided document: not supported by it: “"
            + FLAGGED
            + "”.*",
        )
        self.assertEqual(len(calls.payloads), 1)
        self.assertEqual(report.outcome, "noted")

    def test_a_rewrite_that_adds_a_link_is_not_kept(self) -> None:
        added = FLAGGED + " See https://example.org/bridge."
        calls = Calls(CAUGHT, {"text": json.dumps({"1": added}), "cost_eur": "0"})
        text, addition, report = review(CAUSAL, calls)
        self.assertEqual(text, CAUSAL)
        self.assertEqual(report.rewrite, "rejected:links")
        self.assertIn(FLAGGED, addition)

    def test_a_json_answer_is_not_checked(self) -> None:
        # A note or a rewrite would break what the client parses.
        for answer in (
            '{"deck": "steel", "opened": 2020, "cause": "flood of May 2020"}',
            '```json\n{"deck": "steel", "opened": 2020}\n```',
        ):
            calls = Calls()
            text, addition, report = review(answer, calls)
            self.assertEqual((text, addition), (answer, ""))
            self.assertEqual(report.outcome, "skipped:structured")
            self.assertEqual(calls.payloads, [])
        self.assertFalse(faith.structured("The deck {is} steel."))

    def test_a_failed_judge_delivers_the_answer_and_names_the_failure(self) -> None:
        calls = Calls(GatewayError("cost_budget", 429, "limit"))
        text, addition, report = review(FAITHFUL, calls)
        self.assertEqual((text, addition), (FAITHFUL, ""))
        self.assertEqual(report.outcome, "failed:cost_budget")
        calls = Calls(TimeoutError())
        self.assertEqual(review(FAITHFUL, calls)[2].outcome, "failed:timeout")
        calls = Calls({"text": "not json", "cost_eur": "0.004"})
        _, _, report = review(FAITHFUL, calls)
        self.assertEqual(
            (report.outcome, report.cost_eur), ("failed:unreadable", Decimal("0.004"))
        )

    def test_a_failed_judge_still_says_what_code_did_not_find(self) -> None:
        answer = FAITHFUL + " It cost 15 million euros."
        text, addition, report = review(answer, Calls(TimeoutError()))
        self.assertEqual(text, answer)
        self.assertEqual(
            addition, "*Not found in the provided document: “15 million”.*"
        )
        # The judge failed, so the numbers are all the answer can say about.
        self.assertEqual(report.outcome, "failed:timeout")
        self.assertEqual(report.hints, 1)

    def test_a_source_too_long_for_the_judge_is_not_judged(self) -> None:
        sources = document_quotes.Sources()
        sources.add_document(SOURCE * 400, "long.txt")
        long_source = faith.provided(sources)
        calls = Calls()
        text, addition, report = review(FAITHFUL, calls, source=long_source)
        self.assertEqual((text, addition), (FAITHFUL, ""))
        self.assertEqual(report.outcome, "skipped:source_too_long")
        self.assertEqual(calls.payloads, [])

    def test_no_time_left_is_no_call(self) -> None:
        calls = Calls()
        _, _, report = review(FAITHFUL, calls, seconds=8)
        self.assertEqual(report.outcome, "skipped:deadline")
        self.assertEqual(calls.payloads, [])

    def test_too_little_time_for_a_repair_gives_the_note_instead(self) -> None:
        calls = Calls(CAUGHT)
        _, addition, report = review(CAUSAL, calls, seconds=40)
        self.assertEqual(report.rewrite, "skipped:deadline")
        self.assertIn(FLAGGED, addition)

    def test_log_mode_changes_nothing_and_off_mode_calls_nothing(self) -> None:
        calls = Calls(CAUGHT)
        text, addition, report = review(CAUSAL, calls, setting="log")
        self.assertEqual((text, addition, report.outcome), (CAUSAL, "", "logged"))
        calls = Calls()
        self.assertEqual(review(CAUSAL, calls, setting="off")[:2], (CAUSAL, ""))
        self.assertEqual(calls.payloads, [])

    def test_strict_privacy_keeps_the_turn_profile(self) -> None:
        calls = Calls(CLEAN)
        with patch("packages.privacy.owned_only", return_value=True):
            review(FAITHFUL, calls)
        self.assertEqual(calls.payloads[0]["profile"], "atlas-qwen")
        self.assertEqual(calls.payloads[0]["privacy"], "strict")


@WITHOUT_SCREEN
class PipelineTests(unittest.IsolatedAsyncioTestCase):
    """The chat and API paths share chat_pipeline.process."""

    async def turn(
        self, calls: Calls, *, atlas_ui: bool, pasted: str = SOURCE
    ) -> tuple[Interaction, list[str]]:
        async def generate(request: ChatRequest, item: Interaction) -> None:
            upstream = sink_context.get()
            assert upstream is not None
            await upstream({"delta": {"content": CAUSAL}})
            item.reponse = CAUSAL
            item.state = "done"
            item.cout_eur = 0.01

        sent: list[str] = []

        async def sink(event: dict[str, object]) -> None:
            delta = event.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                sent.append(str(delta["content"]))

        request = ChatRequest.model_validate(
            {
                "messages": [{"role": "user", "content": pasted + "\n\n" + QUESTION}],
                "atlas_ui": atlas_ui,
                "stream": True,
            }
        )
        item = Interaction()
        token = sink_context.set(sink)
        try:
            with (
                patch.dict(os.environ, {"ATLAS_SOURCE_FAITHFULNESS": "on"}),
                patch(
                    "services.orchestrator.chat_pipeline._process_request",
                    side_effect=generate,
                ),
                patch.object(faith, "_complete", calls),
            ):
                await process(request, item)
        finally:
            sink_context.reset(token)
        return item, sent

    async def test_an_api_answer_is_rewritten_before_it_is_sent(self) -> None:
        fixed = (
            "The town council chose a steel deck because it weighed less than concrete."
        )
        calls = Calls(
            CAUGHT, {"text": json.dumps({"1": fixed}), "cost_eur": "0.001"}, CLEAN
        )
        item, _ = await self.turn(calls, atlas_ui=False)
        self.assertEqual(item.reponse, CAUSAL.replace(FLAGGED, fixed))
        self.assertAlmostEqual(item.cout_eur, 0.023)
        step = item.steps[-1]
        self.assertEqual((step["phase"], step["outcome"]), ("source_check", "revised"))

    async def test_a_streamed_answer_gets_the_note_in_the_stream_and_the_stored_answer(
        self,
    ) -> None:
        item, sent = await self.turn(Calls(CAUGHT), atlas_ui=True)
        self.assertEqual("".join(sent[:-1]), CAUSAL)
        self.assertTrue(
            sent[-1].startswith("\n\n*Checked against the provided document")
        )
        self.assertEqual(item.reponse, CAUSAL + sent[-1])

    async def test_a_short_message_without_document_is_not_checked(self) -> None:
        calls = Calls()
        item, _ = await self.turn(
            calls, atlas_ui=False, pasted="Tell me about bridges."
        )
        self.assertEqual(item.reponse, CAUSAL)
        self.assertEqual(calls.payloads, [])


@WITHOUT_SCREEN
class DeliverableTests(unittest.IsolatedAsyncioTestCase):
    """A Work file derived from a provided source: unsupported statements are gaps."""

    async def test_unsupported_statements_of_a_file_are_refused_once(self) -> None:
        from services.orchestrator import deliverable_check

        model = SimpleNamespace(
            profile="atlas-qwen",
            strict=True,
            spent=Decimal(0),
            allowance=Decimal("0.1"),
        )
        tools = SimpleNamespace(
            check_model=model,
            request_text="Write a one-page summary of the attached document as a docx file.",
            earlier_requests=[],
            deliverable_checks=0,
            deliverable_refused=False,
        )
        sources = document_quotes.Sources()
        sources.add_document(SOURCE, "bridge.txt")

        async def complete(payload: dict[str, Any], seconds: float) -> dict[str, Any]:
            if "Compare the SUMMARY" in str(payload["messages"][0]["content"]):
                return dict(CAUGHT)
            return {"text": '{"gaps": [], "variants": []}', "cost_eur": "0.001"}

        token = document_quotes.current.set(sources)
        try:
            outcome = await deliverable_check.review(
                tools,
                ".txt",
                None,
                CAUSAL.encode(),
                CAUSAL.encode(),
                [],
                complete=complete,
            )
        finally:
            document_quotes.current.reset(token)
        self.assertEqual(len(outcome.unsupported), 1)
        self.assertIn(FLAGGED, outcome.unsupported[0])
        self.assertEqual(outcome.cost_eur, Decimal("0.007"))
        with self.assertRaises(ValueError) as refused:
            deliverable_check.decide(tools, outcome)
        self.assertIn(FLAGGED, str(refused.exception))
        # The repair is the only one: the next file carries a note instead.
        self.assertIn(FLAGGED, deliverable_check.decide(tools, outcome))

    async def test_a_file_without_a_provided_source_has_no_source_check(self) -> None:
        from services.orchestrator import deliverable_check

        self.assertIsNone(
            deliverable_check.source_check(
                SimpleNamespace(), "a request", CAUSAL, "atlas-qwen", 40, AsyncMock()
            )
        )


class ScreenTests(unittest.TestCase):
    """The fast screen first; the judge decides what it flags."""

    roles = {
        "source_faithfulness": "atlas-judge",
        "source_faithfulness_screen": "atlas-fast",
        "source_faithfulness_rewrite": "atlas-judge",
    }

    def run_review(self, answer: str, calls: Calls) -> tuple[str, str, faith.Report]:
        with patch("packages.profiles.picture_profile", side_effect=self.roles.get):
            return review(answer, calls)

    def test_an_answer_the_screen_finds_clean_is_done_after_one_call(self) -> None:
        calls = Calls(CLEAN)
        text, addition, report = self.run_review(FAITHFUL, calls)
        self.assertEqual((text, addition, report.outcome), (FAITHFUL, "", "clean"))
        self.assertEqual([p["profile"] for p in calls.payloads], ["atlas-fast"])
        self.assertEqual(report.screen, ["clean"])
        self.assertEqual(report.cost_eur, Decimal("0.006"))

    def test_what_the_screen_flags_is_decided_by_the_judge(self) -> None:
        # A false alarm of the screen: the judge finds the answer faithful.
        calls = Calls(CAUGHT, CLEAN)
        text, addition, report = self.run_review(CAUSAL, calls)
        self.assertEqual((text, addition, report.outcome), (CAUSAL, "", "clean"))
        self.assertEqual(
            [p["profile"] for p in calls.payloads], ["atlas-fast", "atlas-judge"]
        )
        # Both read the same measured prompt.
        screened, judged_by = (p["messages"][0]["content"] for p in calls.payloads)
        self.assertEqual(screened, judged_by)
        self.assertEqual(report.screen, ["flagged"])
        self.assertEqual(report.cost_eur, Decimal("0.012"))
        self.assertEqual(report.calls, 1)

    def test_a_confirmed_finding_is_rewritten_and_the_rewrite_screened(self) -> None:
        fixed = (
            "The town council chose a steel deck because it weighed less than concrete."
        )
        calls = Calls(
            CAUGHT,
            CAUGHT,
            {"text": json.dumps({"1": fixed}), "cost_eur": "0.001"},
            CLEAN,
        )
        text, addition, report = self.run_review(CAUSAL, calls)
        self.assertEqual(text, CAUSAL.replace(FLAGGED, fixed))
        self.assertEqual((addition, report.outcome), ("", "revised"))
        self.assertEqual(
            [p["profile"] for p in calls.payloads],
            ["atlas-fast", "atlas-judge", "atlas-judge", "atlas-fast"],
        )
        self.assertEqual(report.screen, ["flagged", "clean"])

    def test_a_failed_screen_leaves_the_decision_to_the_judge(self) -> None:
        calls = Calls(TimeoutError(), CAUGHT, TimeoutError())
        text, addition, report = self.run_review(CAUSAL, calls)
        self.assertEqual(report.screen[0], "failed:timeout")
        self.assertIn(FLAGGED, text + addition)
        self.assertEqual(
            [p["profile"] for p in calls.payloads][:2], ["atlas-fast", "atlas-judge"]
        )

    def test_no_screen_in_strict_privacy_or_on_the_judge_profile(self) -> None:
        with patch("packages.profiles.picture_profile", side_effect=self.roles.get):
            self.assertEqual(faith.screen_profile("atlas-qwen"), "atlas-fast")
            with patch.object(faith.privacy, "owned_only", return_value=True):
                self.assertIsNone(faith.screen_profile("atlas-qwen"))
        same = {**self.roles, "source_faithfulness_screen": "atlas-judge"}
        with patch("packages.profiles.picture_profile", side_effect=same.get):
            self.assertIsNone(faith.screen_profile("atlas-qwen"))


class RoleTests(unittest.TestCase):
    def test_both_roles_name_a_declared_profile(self) -> None:
        from pathlib import Path

        import yaml

        from packages.profiles import HIDDEN_PROFILES, PICTURE_ROLES, PROFILES

        roles = yaml.safe_load(Path("services/model-gateway/routing.yaml").read_text())[
            "picture_roles"
        ]
        for role in (
            "source_faithfulness",
            "source_faithfulness_screen",
            "source_faithfulness_rewrite",
        ):
            self.assertIn(role, PICTURE_ROLES)
            self.assertIn(roles[role], (*PROFILES, *HIDDEN_PROFILES))


if __name__ == "__main__":
    unittest.main()
