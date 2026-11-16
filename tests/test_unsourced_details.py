"""Unsupported precise details: extraction, lookup, revision checks (fictional names)."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import os
import unittest

from services.orchestrator import encyclopedia, grounding, reference_fetch
from services.orchestrator import unsourced_details as details
from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.stream_client import sink_context
from services.orchestrator.dictation_cleanup import fast_profile
from services.orchestrator.unsourced_details import Evidence

SOURCE = (
    "Velmora Aerotech was founded on 18 December 1970 in Brenzac by Corvane "
    "Industries and Halsted Works. It employs 47 120 people. The founders were "
    "two engineers, Ilse Varnholt and Tomas Quedrin. Wikidata inception: 1970-12-18."
)


def unsupported(
    answer: str, passages: list[str] = [SOURCE], own: str = ""
) -> list[str]:
    found = details.unsupported(answer, Evidence.of(passages), Evidence.of([own]))
    return [detail.text for detail in found]


def check_extracts_dates_numbers_names_and_titles() -> None:
    answer = (
        "Velmora Aerotech was founded on 18 December 1970 by Corvane Industries. "
        "Its first model, “Skylark Nine”, flew in 1974 with 120 seats."
    )
    found = {(d.kind, d.text) for d in details.extract(answer)}
    assert ("date", "18 December 1970") in found
    assert ("year", "1974") in found
    assert ("number", "120") in found
    assert ("name", "Corvane Industries") in found
    assert ("title", "Skylark Nine") in found


def check_sourced_details_in_other_written_forms_are_supported() -> None:
    answer = (
        "Velmora Aerotech a été fondée le 18 décembre 1970 à Brenzac. "
        "Elle emploie environ 47 000 personnes et compte deux fondateurs."
    )
    assert unsupported(answer) == []


def check_details_added_from_memory_are_found() -> None:
    answer = (
        "Velmora Aerotech was founded in 1970 by Corvane Industries, Halsted Works "
        "and Drummel Avionics. Its headquarters moved to Ostrava in 1988."
    )
    assert sorted(unsupported(answer)) == ["1988", "Drummel Avionics", "Ostrava"]


def check_a_title_before_a_sourced_name_is_supported() -> None:
    passages = ["Corvane Industries was led by Ilse Varnholt from 1971."]
    assert unsupported("It was led by President Ilse Varnholt.", passages) == []


def check_question_words_and_today_are_not_added_details() -> None:
    answer = "Velmora Aerotech, which you asked about, was founded in 1970 in Brenzac."
    assert unsupported(answer, own="Who founded Velmora Aerotech?") == []


def check_age_from_two_supported_years_is_supported() -> None:
    passages = ["Ilse Varnholt (born 1931, died 2014) was an engineer."]
    assert unsupported("Ilse Varnholt (1931-2014) died at 83.", passages) == []
    assert unsupported("Ilse Varnholt (1931-2014) died at 61.", passages) == ["61"]


def check_headings_and_sentence_openers_are_not_names() -> None:
    answer = (
        "## Release Date\n"
        "**Inauguration :** 1970.\n"
        "Founded in Brenzac, the company grew fast.\n"
        "| Spec | Value |\n|---|---|\n| Weight | 500 g |"
    )
    assert unsupported(answer) == ["500"]


def check_abbreviation_does_not_split_a_sentence() -> None:
    body = "The patent (No. 2,555,111) was granted in 1942. Later it was used."
    spans = [body[a:b].strip() for a, b in details._sentences(body)]
    assert spans == [
        "The patent (No. 2,555,111) was granted in 1942.",
        "Later it was used.",
    ]


def check_extraction_is_linear_on_long_answers() -> None:
    for text in ("A " * 60_000, "1," * 60_000, "Abc de " * 30_000, "« " * 60_000):
        started = time.monotonic()
        found = details.unsupported(text, Evidence.of([text]), Evidence.of([""]))
        assert time.monotonic() - started < 3
        assert len(found) <= details.MAX_DETAILS


def check_wants_review_only_when_enabled() -> None:
    with patch.dict(os.environ, {"ATLAS_UNSOURCED_DETAILS": "off"}):
        assert not details.wants_review("tell me about Velmora Aerotech")
    with patch.dict(os.environ, {"ATLAS_UNSOURCED_DETAILS": "revise"}):
        _enabled_review_gate()


def _enabled_review_gate() -> None:
    assert details.wants_review("tell me about Velmora Aerotech")
    assert details.wants_review("parle moi de Ilse Varnholt")
    assert details.wants_review("wich year was Velmora founded")
    assert not details.wants_review("imagine a story about Velmora Aerotech")
    assert not details.wants_review("merci")
    # A definition or an explanation without a named subject keeps streaming.
    assert not details.wants_review(
        "c'est quoi la difference entre un virus et une bacterie"
    )
    assert not details.wants_review("what are the best rated budget laptops right now")


def gateway(text: str) -> AsyncMock:
    return AsyncMock(return_value={"text": text, "cost_eur": "0.0002"})


def run(
    answer: str, setting: str, mock: AsyncMock, passages: list[str] = [SOURCE]
) -> tuple[str, details.Report]:
    with patch.object(details, "_complete", mock):
        return asyncio.run(
            details.review(
                answer,
                passages,
                ["Who founded Velmora Aerotech?"],
                profile="atlas-test",
                language="en",
                setting=setting,
                reference=False,
            )
        )


ANSWER = (
    "Velmora Aerotech was founded in 1970 by Corvane Industries and Halsted Works.\n"
    "- Drummel Avionics (Ostrava) joined as a third founder.\n"
    "It employs 47 120 people.\n\n<details>\n<summary>Sources</summary>\n\n"
    "- <https://example.org/velmora>\n</details>"
)


def check_off_changes_nothing() -> None:
    mock = gateway("{}")
    answer, report = run(ANSWER, "off", mock)
    assert answer == ANSWER and report.unsupported == 0
    mock.assert_not_awaited()


def check_revision_removes_unsupported_sentence_and_keeps_the_rest() -> None:
    answer, report = run(ANSWER, "revise", gateway('{"1": ""}'))
    assert "Drummel" not in answer and "Ostrava" not in answer
    assert answer.startswith(
        "Velmora Aerotech was founded in 1970 by Corvane Industries"
    )
    assert "It employs 47 120 people." in answer
    assert answer.endswith("</details>")
    assert report.revised == "kept" and report.unsupported_after == 0
    assert str(report.cost_eur) == "0.0002"


def _payload_of(mock: AsyncMock) -> dict[str, object]:
    with patch.object(details, "_complete", mock):
        asyncio.run(
            details.review(
                ANSWER,
                [SOURCE],
                [],
                profile="atlas-test",
                language="en",
                setting="revise",
                reference=False,
            )
        )
    assert mock.await_args is not None
    payload: dict[str, object] = mock.await_args.args[0]
    return payload


def check_revision_payload_lists_only_the_sentences_to_correct() -> None:
    payload = _payload_of(gateway('{"1": ""}'))
    messages = payload["messages"]
    assert isinstance(messages, list)
    content = messages[1]["content"]
    assert "[1] - Drummel Avionics (Ostrava) joined as a third founder." in content
    assert "unsupported: Drummel Avionics; Ostrava" in content
    assert (
        "Corvane Industries and Halsted Works"
        not in content.split("SOURCE EXCERPTS")[0]
    )
    assert payload["profile"] == fast_profile() and payload["tools"] == []


def check_revision_adding_a_detail_is_rejected() -> None:
    answer, report = run(
        ANSWER, "revise", gateway('{"1": "- Pellow Systems joined in 1972."}')
    )
    assert answer == ANSWER
    assert report.revised == "rejected:new_details"


def check_revision_adding_a_link_is_rejected() -> None:
    answer, report = run(
        ANSWER,
        "revise",
        gateway('{"1": "- A [third partner](https://example.org/x) joined."}'),
    )
    assert answer == ANSWER and report.revised == "rejected:links"


def check_unreadable_or_late_revision_keeps_the_answer() -> None:
    answer, report = run(ANSWER, "revise", gateway("no json here"))
    assert answer == ANSWER and report.revised == "failed:format"
    answer, report = run(ANSWER, "revise", AsyncMock(side_effect=TimeoutError))
    assert answer == ANSWER and report.revised == "late"


def check_revise_mark_notes_what_remains_in_the_answer_language() -> None:
    answer, report = run(ANSWER, "revise_mark", gateway("{}"))
    assert report.marked == 1
    assert (
        "joined as a third founder. *(not found in the sources consulted: "
        "Drummel Avionics, Ostrava)*\n" in answer
    )


def check_reference_article_supports_details_and_joins_the_sources() -> None:
    article = {
        "query": {
            "pages": [
                {
                    "title": "Velmora Aerotech",
                    "canonicalurl": "https://en.wikipedia.org/wiki/Velmora_Aerotech",
                    "extract": "In 1971 Drummel Avionics, based in Ostrava, joined.",
                }
            ]
        }
    }
    fetch = AsyncMock(return_value=json.dumps(article).encode())
    mock = gateway("{}")
    with (
        patch.object(encyclopedia, "source_plan", return_value="live"),
        patch.object(reference_fetch, "get", fetch),
        patch.object(details, "_complete", mock),
    ):
        answer, report = asyncio.run(
            details.review(
                ANSWER,
                [SOURCE],
                [],
                profile="atlas-test",
                language="en",
                question="Who founded Velmora Aerotech?",
                urls=["https://en.wikipedia.org/wiki/Velmora_Aerotech"],
                setting="revise",
            )
        )
    mock.assert_not_awaited()
    assert report.referenced == 2 and report.revised == "skipped:none"
    assert "- <https://en.wikipedia.org/wiki/Velmora_Aerotech>\n</details>" in answer
    assert fetch.await_args is not None
    assert fetch.await_args.kwargs["params"]["titles"] == "Velmora Aerotech"


def check_strict_level_reads_no_live_reference() -> None:
    with patch.object(encyclopedia, "source_plan", return_value="local"):
        assert (
            asyncio.run(details.references("Who founded Velmora?", "en", [], [])) == []
        )


class UnsourcedDetailsTests(unittest.TestCase):
    def test_extracts_dates_numbers_names_and_titles(self) -> None:
        check_extracts_dates_numbers_names_and_titles()

    def test_sourced_details_in_other_written_forms_are_supported(self) -> None:
        check_sourced_details_in_other_written_forms_are_supported()

    def test_details_added_from_memory_are_found(self) -> None:
        check_details_added_from_memory_are_found()

    def test_a_title_before_a_sourced_name_is_supported(self) -> None:
        check_a_title_before_a_sourced_name_is_supported()

    def test_question_words_and_today_are_not_added_details(self) -> None:
        check_question_words_and_today_are_not_added_details()

    def test_age_from_two_supported_years_is_supported(self) -> None:
        check_age_from_two_supported_years_is_supported()

    def test_headings_and_sentence_openers_are_not_names(self) -> None:
        check_headings_and_sentence_openers_are_not_names()

    def test_abbreviation_does_not_split_a_sentence(self) -> None:
        check_abbreviation_does_not_split_a_sentence()

    def test_extraction_is_linear_on_long_answers(self) -> None:
        check_extraction_is_linear_on_long_answers()

    def test_wants_review_only_when_enabled(self) -> None:
        check_wants_review_only_when_enabled()

    def test_off_changes_nothing(self) -> None:
        check_off_changes_nothing()

    def test_revision_removes_unsupported_sentence_and_keeps_the_rest(self) -> None:
        check_revision_removes_unsupported_sentence_and_keeps_the_rest()

    def test_revision_payload_lists_only_the_sentences_to_correct(self) -> None:
        check_revision_payload_lists_only_the_sentences_to_correct()

    def test_revision_adding_a_detail_is_rejected(self) -> None:
        check_revision_adding_a_detail_is_rejected()

    def test_revision_adding_a_link_is_rejected(self) -> None:
        check_revision_adding_a_link_is_rejected()

    def test_unreadable_or_late_revision_keeps_the_answer(self) -> None:
        check_unreadable_or_late_revision_keeps_the_answer()

    def test_revise_mark_notes_what_remains_in_the_answer_language(self) -> None:
        check_revise_mark_notes_what_remains_in_the_answer_language()

    def test_reference_article_supports_details_and_joins_the_sources(self) -> None:
        check_reference_article_supports_details_and_joins_the_sources()

    def test_strict_level_reads_no_live_reference(self) -> None:
        check_strict_level_reads_no_live_reference()


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    """The treatment runs on a held factual turn, before grounding, only when enabled."""

    async def turn(self, setting: str, question: str) -> tuple[Interaction, AsyncMock]:
        answer = (
            "Velmora Aerotech was founded in 1970 by Corvane Industries.\n"
            "- Drummel Avionics joined in 1988."
        )

        async def generate(request: ChatRequest, item: Interaction) -> None:
            upstream = sink_context.get()
            assert upstream is not None
            if (check := grounding.current.get()) is not None:
                check.collect({"results": [{"snippet": SOURCE}]})
            await upstream({"delta": {"content": answer}})
            item.reponse = answer
            item.state = "done"
            item.cout_eur = 0.01

        async def sink(event: dict[str, object]) -> None:
            return None

        mock = gateway('{"1": ""}')
        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": question}]}
        )
        item = Interaction()
        token = sink_context.set(sink)
        try:
            with (
                patch.dict(
                    os.environ,
                    {"ATLAS_GROUNDING": "on", "ATLAS_UNSOURCED_DETAILS": setting},
                ),
                patch(
                    "services.orchestrator.chat_pipeline._process_request",
                    side_effect=generate,
                ),
                patch.object(details, "_complete", mock),
                patch.object(details, "references", AsyncMock(return_value=[])),
            ):
                await process(request, item)
        finally:
            sink_context.reset(token)
        return item, mock

    async def test_enabled_turn_drops_the_unsupported_line(self) -> None:
        item, mock = await self.turn("revise", "Who founded Velmora Aerotech?")
        mock.assert_awaited_once()
        self.assertEqual(
            item.reponse,
            "Velmora Aerotech was founded in 1970 by Corvane Industries.",
        )
        self.assertAlmostEqual(item.cout_eur, 0.0102)

    async def test_disabled_turn_is_unchanged(self) -> None:
        item, mock = await self.turn("off", "Who founded Velmora Aerotech?")
        mock.assert_not_awaited()
        self.assertIn("Drummel Avionics", item.reponse)

    async def test_question_without_fact_cue_is_held_when_enabled(self) -> None:
        item, mock = await self.turn("revise", "tell me about velmora aerotech")
        mock.assert_awaited_once()
        self.assertNotIn("Drummel Avionics", item.reponse)


if __name__ == "__main__":
    unittest.main()
