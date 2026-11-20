"""Computed results stay in the answer; computations run no required search.

Staging 2026-11-20: the details review cut the date_time day count out of "There
are 261 days between..." (leaving "There are **days**"), and a percentage
question ran a required web search whose unrelated hits were listed under the
answer. Fictional names only.
"""

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator import grounding, source_truth
from services.orchestrator import unsourced_details as details
from services.orchestrator.chat_pipeline import ChatTools, process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call
from services.orchestrator.search_policy import computation_only, required_research
from services.orchestrator.stream_client import sink_context
from services.orchestrator.unsourced_details import Computed, Evidence

TODAY = "2026-11-20"
DAYS_QUESTION = "How many days are there between 3 March 2026 and 19 November 2026?"
DAYS_ANSWER = (
    "There are **261** days between 3 March 2026 and 19 November 2026 (counting "
    "from the day after 3 March up to and including 19 November). If you count "
    "both endpoint days inclusively, that is 262 days."
)
DAYS_TOOL = json.dumps(
    {
        "start": "2026-03-03",
        "end": "2026-11-19",
        "days": 261,
        "days_counting_both_ends": 262,
        "weeks": 37,
        "remaining_days_after_weeks": 2,
    }
)
PERCENT_QUESTION = (
    "Un article coûte 84 euros, il passe à 63 euros. "
    "Quel est le pourcentage de baisse ?"
)
PERCENT_ANSWER = (
    "Le pourcentage de baisse est **25 %**.\n\nDétail du calcul : la baisse est "
    "de 84 − 63 = 21 euros, rapportée au prix initial de 84 euros : "
    "21 / 84 × 100 = **25 %**."
)


def missing(
    answer: str,
    question: str,
    results: list[str] = [],
    passages: list[str] = [],
) -> list[str]:
    computed = Computed.of([question], results, TODAY)
    own = Evidence.of([question, *results, TODAY])
    found = details.unsupported(answer, Evidence.of(passages), own, computed)
    return [detail.text for detail in found]


class ComputedValuesTests(unittest.TestCase):
    def test_date_tool_result_and_calendar_span_are_supported(self) -> None:
        self.assertEqual(missing(DAYS_ANSWER, DAYS_QUESTION, [DAYS_TOOL]), [])
        # Without the tool, the span between the two given dates is a calendar fact.
        self.assertEqual(missing(DAYS_ANSWER, DAYS_QUESTION), [])
        weeks = "That is about 37 weeks, or 37.3 weeks, about 8 months."
        self.assertEqual(missing(weeks, DAYS_QUESTION), [])

    def test_days_until_a_date_count_from_today(self) -> None:
        question = "Combien de jours reste-t-il avant le 25 décembre 2026 ?"
        self.assertEqual(
            missing("Il reste 35 jours avant le 25 décembre 2026.", question), []
        )

    def test_percentage_from_the_persons_numbers_is_supported(self) -> None:
        self.assertEqual(missing(PERCENT_ANSWER, PERCENT_QUESTION), [])

    def test_arithmetic_in_several_languages(self) -> None:
        cases = (
            (
                "Ein Artikel kostet 120 Euro und wird um 15 % reduziert. "
                "Wie viel kostet er jetzt?",
                "Er kostet jetzt 102 Euro (120 × 0,85 = 102), also 18 Euro weniger.",
            ),
            (
                "Ho speso 45,50 euro e 30,25 euro. Quanto ho speso in totale?",
                "Hai speso in totale 75,75 euro.",
            ),
            (
                "Si gano 2.400 euros al mes y gasto 1.800, ¿qué porcentaje ahorro?",
                "Ahorras 600 euros al mes, es decir un 25 % de tus ingresos.",
            ),
            (
                "A 3 400 € budget is split between 4 teams. How much per team?",
                "Each team gets 850 € (3 400 / 4 = 850).",
            ),
        )
        for question, answer in cases:
            with self.subTest(question=question):
                self.assertEqual(missing(answer, question), [])

    def test_unit_and_currency_results_rounded_by_the_answer(self) -> None:
        unit = json.dumps({"value": "26.2", "result": "42.1648128", "to_unit": "km"})
        self.assertEqual(
            missing(
                "26,2 millas son unos 42,16 km (unos 42 km).",
                "¿Cuántos kilómetros son 26,2 millas?",
                [unit],
            ),
            [],
        )
        rate = json.dumps(
            {
                "amount": "250",
                "from": "USD",
                "to": "EUR",
                "result": "214.603",
                "rate": "0.858412",
            }
        )
        self.assertEqual(
            missing(
                "250 USD font environ 214,60 EUR au taux de 0,8584.",
                "Combien font 250 dollars en euros ?",
                [rate],
            ),
            [],
        )

    def test_weekday_names_are_calendar_facts(self) -> None:
        answer = "19 November 2026 is a Thursday, and 3 March 2026 was a Tuesday."
        self.assertEqual(missing(answer, DAYS_QUESTION), [])

    def test_details_from_memory_are_still_found(self) -> None:
        answer = "84 minus 63 is 21. Velmora Aerotech employs 9 731 people in Brenzac."
        self.assertEqual(
            sorted(missing(answer, "What is 84 minus 63?")),
            ["9 731", "Brenzac", "Velmora Aerotech"],
        )
        # A wrong written calculation is no computed result.
        self.assertEqual(
            missing("84 − 63 = 27.", "What is 84 minus 63? Also 5 and 7."), ["27"]
        )

    def test_computation_is_bounded_on_long_inputs(self) -> None:
        question = " ".join(str(number) for number in range(50_000))
        started = time.monotonic()
        computed = Computed.of([question], [question], TODAY)
        self.assertLessEqual(len(computed.base), details.BASE_VALUES)
        details.unsupported(question, Evidence(), Evidence(), computed)
        self.assertLess(time.monotonic() - started, 5)


class HollowSentenceTests(unittest.TestCase):
    def test_a_number_cut_out_of_its_sentence_is_hollow(self) -> None:
        original = "There are **300** days between 3 March 2026 and 19 November 2026."
        self.assertTrue(
            details.hollow(
                original,
                "There are **days** between 3 March 2026 and 19 November 2026.",
                ["300"],
            )
        )
        self.assertTrue(
            details.hollow(original, "There are **** days between the dates.", ["300"])
        )
        self.assertFalse(
            details.hollow(original, "The two dates are in the same year.", ["300"])
        )
        # A name dropped from a list leaves a list.
        self.assertFalse(
            details.hollow(
                "Founders: Ilse Varnholt, Tomas Quedrin, Pellow Drane.",
                "Founders: Ilse Varnholt, Tomas Quedrin.",
                ["Pellow Drane"],
            )
        )

    def test_revision_never_leaves_a_hollow_sentence(self) -> None:
        answer = (
            "Velmora Aerotech was founded in 1970 in Brenzac.\n"
            "It runs **300** test flights a year from Brenzac.\n"
            "It is based in Brenzac."
        )
        mock = AsyncMock(
            return_value={
                "text": '{"1": "It runs **test flights** a year from Brenzac."}',
                "cost_eur": "0.0001",
            }
        )
        with patch.object(details, "_complete", mock):
            revised, report = asyncio.run(
                details.review(
                    answer,
                    ["Velmora Aerotech was founded in 1970 in Brenzac."],
                    ["Tell me about Velmora Aerotech"],
                    profile="atlas-test",
                    language="en",
                    setting="revise",
                    reference=False,
                )
            )
        self.assertNotIn("test flights", revised)
        self.assertNotIn("****", revised)
        self.assertTrue(
            revised.startswith("Velmora Aerotech was founded in 1970 in Brenzac.\n")
        )
        self.assertTrue(revised.endswith("\nIt is based in Brenzac."))
        self.assertEqual(report.revised, "kept")


def _review(
    answer: str, question: str, articles: list[tuple[str, str]], computed: list[str]
) -> tuple[str, details.Report, AsyncMock, AsyncMock]:
    lookup = AsyncMock(return_value=articles)
    revise = AsyncMock(return_value={"text": "{}", "cost_eur": "0"})
    with (
        patch.object(details, "references", lookup),
        patch.object(details, "_complete", revise),
        patch.object(time, "strftime", return_value=TODAY),
    ):
        result, report = asyncio.run(
            details.review(
                answer,
                [],
                [question],
                profile="atlas-test",
                language="en",
                question=question,
                setting="revise",
                computed=computed,
            )
        )
    return result, report, lookup, revise


class ReviewTests(unittest.TestCase):
    def test_computed_answer_is_kept_without_any_lookup(self) -> None:
        footer = "\n\n<details>\n<summary>Sources</summary>\n\n- <https://x.test/a>\n</details>"
        answer, report, lookup, revise = _review(
            DAYS_ANSWER + footer, DAYS_QUESTION, [], [DAYS_TOOL]
        )
        self.assertEqual(answer, DAYS_ANSWER + footer)
        lookup.assert_not_awaited()
        revise.assert_not_awaited()
        self.assertEqual(report.reference_urls, [])

    def test_the_persons_dates_never_bring_an_unrelated_article(self) -> None:
        answer = (
            (
                "Between 3 March 2026 and 19 November 2026 the Velmora Aerotech plant "
                "in Brenzac stays open."
            )
            + "\n\n<details>\n<summary>Sources</summary>\n\n- <https://x.test/a>\n</details>"
        )
        articles = [
            (
                "https://en.wikipedia.org/wiki/2026_Iran_war",
                "On 3 March 2026 a ceasefire was announced. Brenzac.",
            ),
            (
                "https://en.wikipedia.org/wiki/Velmora_Aerotech",
                "Velmora Aerotech has a plant in Brenzac.",
            ),
        ]
        result, report, lookup, _ = _review(answer, DAYS_QUESTION, articles, [])
        lookup.assert_awaited_once()
        self.assertEqual(
            report.reference_urls, ["https://en.wikipedia.org/wiki/Velmora_Aerotech"]
        )
        self.assertNotIn("Iran_war", result)

    def test_an_article_must_be_about_the_subject(self) -> None:
        self.assertFalse(
            details.about_subject(
                "https://fr.wikipedia.org/wiki/Stade_de_Penvillers",
                PERCENT_QUESTION,
                PERCENT_ANSWER,
            )
        )
        self.assertFalse(
            details.about_subject(
                "https://en.wikipedia.org/wiki/2026_Iran_war",
                DAYS_QUESTION,
                DAYS_ANSWER,
            )
        )
        self.assertTrue(
            details.about_subject(
                "https://en.wikipedia.org/wiki/Velmora_Aerotech",
                "Who founded Velmora Aerotech?",
                "",
            )
        )
        self.assertTrue(
            details.about_subject(
                "https://en.wikipedia.org/wiki/Bank_of_Velmora_(central_bank)",
                "Qui dirige la Banque de Velmora ?",
                "",
            )
        )
        self.assertTrue(details.about_subject("https://x.test/page", "q", "a"))


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_computation_tool_result_reaches_the_review(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_WEB_CACHE": root + "/cache.sqlite"}),
        ):
            check = grounding.Grounding()
            token = grounding.current.set(check)
            try:
                tools = ChatTools(Interaction())
                await tools.execute(
                    Call("1", "calculator", '{"expr":"(84-63)/84*100"}'), 1
                )
                await tools.execute(
                    Call(
                        "2",
                        "date_time",
                        json.dumps(
                            {
                                "operation": "difference",
                                "date": "2026-03-03",
                                "end_date": "2026-11-19",
                            }
                        ),
                    ),
                    1,
                )
            finally:
                grounding.current.reset(token)
        self.assertEqual(len(check.computed), 2)
        self.assertIn("25", check.computed[0])
        self.assertIn('"days": 261', check.computed[1])
        self.assertEqual(check.evidence, [])

    async def test_the_day_count_survives_the_details_review(self) -> None:
        async def generate(request: ChatRequest, item: Interaction) -> None:
            upstream = sink_context.get()
            assert upstream is not None
            check = grounding.current.get()
            assert check is not None
            check.computation({"data": json.loads(DAYS_TOOL)})
            await upstream({"delta": {"content": DAYS_ANSWER}})
            item.reponse, item.state, item.cout_eur = DAYS_ANSWER, "done", 0.01

        async def sink(event: dict[str, object]) -> None:
            return None

        revise = AsyncMock(return_value={"text": '{"1": ""}', "cost_eur": "0"})
        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": DAYS_QUESTION}]}
        )
        item = Interaction()
        token = sink_context.set(sink)
        try:
            with (
                patch.dict(
                    os.environ,
                    {"ATLAS_GROUNDING": "on", "ATLAS_UNSOURCED_DETAILS": "revise"},
                ),
                patch(
                    "services.orchestrator.chat_pipeline._process_request",
                    side_effect=generate,
                ),
                patch.object(details, "_complete", revise),
                patch.object(details, "references", AsyncMock(return_value=[])),
            ):
                await process(request, item)
        finally:
            sink_context.reset(token)
        revise.assert_not_awaited()
        self.assertIn("**261** days", item.reponse)
        self.assertIn("262 days", item.reponse)


class SearchPolicyTests(unittest.TestCase):
    def test_self_contained_computations_require_no_search(self) -> None:
        for question in (
            PERCENT_QUESTION,
            DAYS_QUESTION,
            "Le prix passe de 1 250 € à 1 100 €, de combien a-t-il baissé ?",
            "A jacket costs 80 dollars with a 15 % discount. What is the final price?",
            "Ein Preis steigt von 40 auf 46 Euro. Wie viel Prozent ist das?",
            "Un precio pasa de 50 a 40 euros, ¿cuál es el porcentaje de descuento?",
            "Il prezzo passa da 200 a 150 euro: qual è la percentuale di sconto?",
            "Convert 5 miles to km and 3 miles to km",
        ):
            with self.subTest(question=question):
                self.assertTrue(computation_only(question))
                self.assertEqual(required_research(question, "fr"), ())

    def test_lookups_keep_their_research(self) -> None:
        for question in (
            "Pourquoi le prix du pétrole a baissé de 20 % en 2024 ?",
            "What is the current price of 2 oz of gold?",
            "Combien coûte un iPhone 15 de 256 Go ?",
            "Quel est le taux de change actuel pour 100 USD en EUR ?",
        ):
            with self.subTest(question=question):
                self.assertFalse(computation_only(question))
                self.assertEqual(
                    required_research(question, "fr")[0].name, "web_search"
                )
        # An explicit request to search the web is followed.
        explicit = "Recherche sur le web le pourcentage de baisse de 84 à 63 euros"
        self.assertEqual(required_research(explicit, "fr")[0].name, "web_search")


class ListedSourcesTests(unittest.TestCase):
    def listed(self, pages: dict[str, str], question: str, answer: str) -> set[str]:
        token = source_truth.page_texts.set({})
        try:
            for url, text in pages.items():
                source_truth.record_page(url, text)
            return source_truth.unrelated_pages(
                [*pages, "https://unread.test/x"], question, answer
            )
        finally:
            source_truth.page_texts.reset(token)

    def test_hits_about_nothing_asked_are_left_out(self) -> None:
        pages = {
            "https://x.test/stade": "Stade de Penvillers, enceinte sportive de 4 000 places.",
            "https://x.test/prix": "Le pourcentage de baisse du prix d'un article en euros.",
            "https://x.test/pilules": "Pilules en ligne, prix en euros.",
        }
        hidden = self.listed(pages, PERCENT_QUESTION, PERCENT_ANSWER)
        self.assertEqual(hidden, {"https://x.test/stade", "https://x.test/pilules"})

    def test_cited_pages_and_pages_holding_the_answer_stay(self) -> None:
        pages = {
            "https://x.test/velmora": "Velmora Aerotech, founded in Brenzac, has 47 120 staff.",
            "https://x.test/history": "History of the Brenzac airfield, rebuilt in 1988.",
            "https://x.test/cited": "An unrelated page the answer links to.",
            "https://x.test/other": "Nothing about the subject.",
        }
        answer = (
            "Velmora Aerotech employs 47 120 people; its airfield was rebuilt in "
            "1988 ([1](https://x.test/cited))."
        )
        hidden = self.listed(
            pages, "How many people does Velmora Aerotech employ?", answer
        )
        self.assertEqual(hidden, {"https://x.test/other"})

    def test_pages_in_another_language_all_stay(self) -> None:
        pages = {
            "https://x.test/a": "Lichtgeschwindigkeit",
            "https://x.test/b": "Vakuum",
        }
        self.assertEqual(
            self.listed(pages, "Quelle est la vitesse de la lumière ?", "Environ."),
            set(),
        )

    def test_without_recorded_pages_nothing_is_hidden(self) -> None:
        self.assertEqual(
            source_truth.unrelated_pages(["https://x.test/a"], PERCENT_QUESTION, "x"),
            set(),
        )


if __name__ == "__main__":
    unittest.main()
