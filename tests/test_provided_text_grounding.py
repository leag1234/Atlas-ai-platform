"""What the person gave is a source: no web search on it, no missing-entity note.

Staging 2026-11-18: "Summarize this text in three sentences:" followed on the next
line by a pasted council report started a web search with the report as its query
(the report said "current" and "prices"), and the grounding check then wrote "No
retrieved source mentions this entity." after the summary's sentences, although
every fact came from the pasted text. A walking route got the same note in Spanish
after the address the person gave. Fictional names only.
"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator import encyclopedia
from services.orchestrator import unsourced_details as details
from services.orchestrator.grounding import (
    WORDING,
    Grounding,
    provided,
    statements,
)
from services.orchestrator.link_requests import own_words, pasted_parts
from services.orchestrator.search_policy import required_research

REPORT = (
    "At its meeting on 4 March, the Brennholm town council approved a new protected "
    "cycle lane on Harbour Road. The project will cost 1.2 million euros, of which the "
    "regional mobility fund covers 40 percent. Councillor Mara Velt, who chairs the "
    "transport committee, said the current painted lane is used by about 900 cyclists "
    "a day and that three collisions were recorded there last year.\n\n"
    "Shopkeepers on Harbour Road raised concerns about the loss of 35 parking spaces. "
    "The council agreed to convert the former ferry depot into a car park with 60 "
    "spaces before the works begin, and to keep two delivery bays in front of the "
    "market hall. The latest traffic count will be published on the town website.\n\n"
    "Works are scheduled to start in September and to last about seven months. During "
    "the works, buses on line 4 will be diverted through Mill Street. The council will "
    "hold a public information evening at the Brennholm library on 12 April, where "
    "residents can see the plans and ask questions about prices for the new parking "
    "permits, which remain available at the town hall."
)
INSTRUCTION = "Summarize this text in three sentences:"
SUMMARY = (
    "The Brennholm town council approved a new protected cycle lane on Harbour Road "
    "at its 4 March meeting, a 1.2 million euro project with 40% funded by the "
    "regional mobility fund, replacing a painted lane used by about 900 cyclists "
    "daily. To address shopkeepers' concerns about losing 35 parking spaces, the "
    "council will convert the former ferry depot into a 60-space car park. "
    "Construction starts in September for about seven months, line 4 buses will "
    "divert via Mill Street, and an information evening will be held at the "
    "Brennholm library on 12 April."
)
# Unrelated pages a search returned for the pasted text.
WEB = (
    "Practice test 12: read the passage about a town council and answer questions "
    "1 to 13. The cost of the new bridge was 3 million euros in 2019."
)
MISSING = [table for table in WORDING["missing"].values()]


def notes(text: str) -> list[str]:
    return [note for note in MISSING if note in text]


class PastedTextRoutingTests(unittest.TestCase):
    def test_a_pasted_text_never_forces_a_search_or_becomes_a_query(self) -> None:
        for separator in ("\n", "\n\n", " "):
            with self.subTest(separator=repr(separator)):
                message = INSTRUCTION + separator + REPORT
                self.assertEqual(own_words(message), INSTRUCTION)
                self.assertEqual(required_research(message, "en"), ())

    def test_the_persons_own_question_still_routes_without_the_text(self) -> None:
        message = "What is the current price of a parking permit?\n" + REPORT
        calls = required_research(message, "en")
        self.assertEqual([call.name for call in calls], ["web_search"])
        query = json.loads(calls[0].arguments)["query"]
        self.assertNotIn("Brennholm", query)
        self.assertNotIn("Harbour Road", query)

    def test_pasted_parts_are_what_own_words_leaves_out(self) -> None:
        message = INSTRUCTION + "\n" + REPORT
        self.assertEqual(pasted_parts(message), [REPORT])
        self.assertEqual(pasted_parts("Who founded Northbridge?"), [])
        attached = "Summarize <attached_files>Orlbrix minutes</attached_files>"
        self.assertEqual(own_words(attached), "Summarize ")
        self.assertEqual(
            pasted_parts(attached), ["<attached_files>Orlbrix minutes</attached_files>"]
        )

    def test_the_encyclopedia_lookup_reads_the_persons_own_words(self) -> None:
        search = AsyncMock(return_value=[])
        with (
            patch.object(encyclopedia, "source_plan", return_value="live"),
            patch.object(encyclopedia, "search", search),
        ):
            asyncio.run(details.references(INSTRUCTION + "\n" + REPORT, "en", [], []))
        queries = " ".join(str(call.args[0]) for call in search.await_args_list)
        self.assertNotIn("brennholm", queries.casefold())
        self.assertNotIn("harbour", queries.casefold())


class ProvidedTextGroundingTests(unittest.TestCase):
    def test_summary_of_a_pasted_text_gets_no_missing_entity_note(self) -> None:
        question = INSTRUCTION + "\n" + REPORT
        checker = Grounding()
        checker.add(WEB)
        checked = checker.check(SUMMARY, question, "en", messages=[question])
        self.assertEqual(checked, SUMMARY)

    def test_a_pasted_value_is_not_contradicted_by_the_web(self) -> None:
        question = INSTRUCTION + "\n\n" + REPORT
        checker = Grounding()
        checker.add("The Harbour Road cycle lane project will cost 3 million euros.")
        answer = "The Harbour Road cycle lane project will cost 1.2 million euros."
        checked = checker.check(answer, question, "en", messages=[question])
        self.assertEqual(checked, answer)

    def test_an_attached_file_is_a_source(self) -> None:
        question = "Summarize the attached minutes."
        checker = Grounding()
        checker.add(WEB)
        answer = "Councillor Mara Velt said 900 cyclists use the lane each day."
        checked = checker.check(
            answer, question, "en", messages=[question], documents=[REPORT]
        )
        self.assertEqual(checked, answer)

    def test_a_trip_repeating_the_persons_address_gets_no_note(self) -> None:
        for language, question, answer, source in (
            (
                "es",
                "Estoy en la Calle de Valdorna 20, en Brennholm. "
                "\u00bfC\u00f3mo llego andando al Museo Corvelle?",
                "Para ir andando desde la Calle de Valdorna 20 al Museo Corvelle, "
                "camina 8 minutos hacia el este. El museo est\u00e1 en la Plaza Corvelle 3.",
                "El Museo Corvelle est\u00e1 en la Plaza Corvelle 3 de Brennholm.",
            ),
            (
                "en",
                "I'm staying at 12 Harbour Road, how do I walk to the Corvelle Museum?",
                "From 12 Harbour Road, walk 8 minutes east. "
                "The Corvelle Museum is at 3 Corvelle Square.",
                "The Corvelle Museum is at 3 Corvelle Square.",
            ),
            (
                "fr",
                "Je suis au 12 rue des Tanneurs, \u00e0 Brennholm. "
                "Comment aller \u00e0 pied au mus\u00e9e Corvelle ?",
                "Depuis le 12 rue des Tanneurs, marchez 8 minutes vers l'est. "
                "Le mus\u00e9e Corvelle est au 3 place Corvelle.",
                "Le mus\u00e9e Corvelle est au 3 place Corvelle.",
            ),
        ):
            with self.subTest(language=language):
                checker = Grounding()
                checker.add(source)
                checked = checker.check(answer, question, language, messages=[question])
                self.assertEqual(notes(checked), [])
                self.assertEqual(checked, answer)

    def test_the_persons_statements_and_their_questions(self) -> None:
        self.assertEqual(
            statements("I'm at 12 Harbour Road, Brennholm, how do I get there?"),
            ["I'm at 12 Harbour Road", " Brennholm"],
        )
        for question in (
            "Who is Mara Velt?",
            "Mara Velt, who is she?",
            "Tell me about Mara Velt.",
            "I think Mara Velt chairs the council, is that right?",
            "Je crois que Mara Velt pr\u00e9side le conseil.",
        ):
            with self.subTest(question=question):
                self.assertEqual(provided([question])[1], "")


class UnsupportedEntityStillNotedTests(unittest.TestCase):
    def test_an_entity_the_person_asks_about_keeps_its_note(self) -> None:
        for question in (
            "When was Orlbrix founded?",
            "I think Orlbrix was founded in 1802, when was it founded?",
            "Who founded Orlbrix?\n" + REPORT,
        ):
            with self.subTest(question=question[:40]):
                checker = Grounding()
                checker.add("Valmorel is a region in the north.")
                answer = "Orlbrix was founded in 1802."
                checked = checker.check(answer, question, "en", messages=[question])
                self.assertEqual(checked, answer + "\n\n" + WORDING["missing"]["en"])

    def test_a_web_claim_contradicted_by_a_source_keeps_its_note(self) -> None:
        question = "When did Northbridge open?"
        checker = Grounding()
        checker.add("Northbridge opened in 1924.")
        checked = checker.check(
            "Northbridge opened in 1925.", question, "en", messages=[question]
        )
        self.assertIn(WORDING["contradicted"]["en"], checked)


if __name__ == "__main__":
    unittest.main()
