"""Trip answers on staging -112 (2026-11-17, seven trips on a phone).

A list of distinct trains was "contradicted" by another train of the same
timetable after every item; a line number came from an off-topic page while
two other sites gave another line; search redirects were still cited through
another provider; citations were left doubled or with a stray separator; a
note listed a locution as an unverified stop.
"""

import unittest

from services.orchestrator import redirects, travel_claims
from services.orchestrator.grounding import WORDING, Grounding, designators
from services.orchestrator.travel_rebuild import citations


class ContradictionTests(unittest.TestCase):
    def test_another_train_of_the_timetable_contradicts_nothing(self) -> None:
        checker = Grounding()
        checker.add(
            "Valmora 233 · 07:03 → 09:37 ( 2h 34min ).\nValmora 315.\n"
            "Valmora 1 · 06:00 → 08:08"
        )
        answer = (
            "These trains qualify:\n\n"
            "- Valmora 1: 06:00 → 08:08\n"
            "- Valmora 3: 06:15 → 08:24\n"
            "- Valmora 7: 06:48 → 09:01\n\n"
            "Valmora 13 arrives at 10:00."
        )
        self.assertEqual(checker.check(answer, "which trains", "en"), answer)
        self.assertEqual(checker.claims_changed, 0)

    def test_the_same_train_with_other_times_gets_one_note_under_the_list(
        self,
    ) -> None:
        checker = Grounding()
        checker.add("Valmora 1 arrives at 09:15.\nValmora 3 arrives at 09:15.")
        answer = (
            "Trains:\n\n"
            "- Valmora 1 arrives at 08:08.\n"
            "- Valmora 3 arrives at 08:24.\n"
            "- Valmora 5 arrives at 08:45.\n\n"
            "Have a good trip."
        )
        checked = checker.check(answer, "which trains", "en")
        self.assertEqual(checked.count("A retrieved source contradicts"), 1)
        self.assertNotIn(WORDING["contradicted"]["en"], checked)
        note = checked.index("A retrieved source contradicts “Valmora 1 arrives")
        self.assertLess(checked.index("Valmora 5 arrives at 08:45."), note)
        self.assertLess(note, checked.index("Have a good trip."))
        self.assertIn("> Valmora 1 arrives at 09:15.", checked)

    def test_designators_are_numbered_items_not_dates_or_times(self) -> None:
        self.assertEqual(designators("- Valmora 1: 06:00 → 08:08"), {("valmora", "1")})
        self.assertEqual(
            designators("Opened on November 18 in 1925, at 7:03, ICE 597."),
            {("ice", "597")},
        )


def sydney_like_turn() -> travel_claims.Turn:
    turn = travel_claims.Turn(
        asked=["how do i get to Orlen Airport from here tomorrow, flight at 8am"],
        place="Kestrel Street, Orlen",
    )
    turn.activate()
    turn.collect(
        {
            "link": "https://fair.example.test/en/event/direction/",
            "text": "From Orlen Airport by train: the Harbour Link T2 Line takes "
            "you from the airport to Brannock Station in about 15 minutes.",
        }
    )
    turn.collect(
        {
            "link": "https://transport.example.test/airport",
            "text": "The T8 Airport Line runs to and from Orlen Airport. The station "
            "fee is charged by the Harbour Link company.",
        }
    )
    turn.collect(
        {
            "link": "https://airport-guide.example.test/train",
            "text": "The Harbour Link runs from the airport stations to Brannock "
            "Station in 13 minutes: catch the T8 Airport line.",
        }
    )
    return turn


class DisputedLineTests(unittest.TestCase):
    answer = (
        "Take the train. The Harbour Link T2 Line connects the airport to "
        "Brannock Station in about 15 minutes "
        "([fair.example.test](https://fair.example.test/en/event/direction/)). "
        "Tap on and off with a card."
    )

    def test_a_line_more_sites_contradict_is_left_out(self) -> None:
        checked = sydney_like_turn().check(self.answer, "en")
        self.assertNotIn("T2", checked.split("\n\n")[0])
        self.assertIn("Tap on and off with a card.", checked)
        self.assertIn("disagree on the line", checked)

    def test_the_line_the_sites_give_is_kept(self) -> None:
        answer = self.answer.replace("T2", "T8")
        self.assertEqual(sydney_like_turn().check(answer, "en"), answer)

    def test_a_site_naming_both_lines_is_no_vote(self) -> None:
        turn = travel_claims.Turn(asked=["to Brannock Station"], place="Orlen")
        turn.activate()
        turn.collect(
            {
                "link": "https://fair.example.test/d",
                "text": "The Harbour Link T2 Line goes to Brannock Station.",
            }
        )
        turn.collect(
            {
                "link": "https://hub.example.test/brannock",
                "text": "Brannock Station: lines T1, T2, T4 and T8. Harbour Link.",
            }
        )
        answer = "The Harbour Link T2 Line goes to Brannock Station."
        self.assertEqual(turn.check(answer, "en"), answer)


class RedirectTests(unittest.TestCase):
    def test_every_provider_result_is_cleaned(self) -> None:
        result: dict[str, object] = {
            "provider": "any",
            "results": [
                {"link": "https://www.google.co.uk/goto?url=CAESkAEB"},
                {"link": "https://rail.example.test/a"},
                {"link": "https://www.google.com/url?q=https://rail.example.test/a"},
                {"link": "https://www.google.com/travel/flights/x.html?gl=GB"},
                {
                    "link": "https://www.bing.com/ck/a?u=https%3A%2F%2Fbus.example.test%2F"
                },
            ],
        }
        rows = redirects.results(result)["results"]
        assert isinstance(rows, list)
        self.assertEqual(
            [row["link"] for row in rows],
            ["https://rail.example.test/a", "https://bus.example.test/"],
        )

    def test_cited_redirects_show_their_site_or_go(self) -> None:
        text = (
            "Trains run all day ([google.co.uk](https://www.google.co.uk/goto?url=CAES)). "
            "The first leaves at 6:20 ([fuente](https://www.google.es/goto?url=CAESe)). "
            "See [the guide](https://www.google.com/url?q=https%3A%2F%2Frail.example.test%2Fx) "
            "([source](https://bus.example.test/a))."
        )
        self.assertEqual(
            redirects.cited(text),
            "Trains run all day. The first leaves at 6:20. "
            "See [the guide](https://rail.example.test/x) "
            "([bus.example.test](https://bus.example.test/a)).",
        )


class CitationTests(unittest.TestCase):
    def test_citations_are_joined_once(self) -> None:
        self.assertEqual(
            citations(
                "4 h 50 min ([rail.example.test](https://rail.example.test/x)),, "
                "([bus.example.test](https://bus.example.test/y)). Up to 29."
            ),
            "4 h 50 min ([rail.example.test](https://rail.example.test/x)), "
            "([bus.example.test](https://bus.example.test/y)). Up to 29.",
        )
        self.assertEqual(
            citations(
                "The corridor ([rail.example.test](https://rail.example.test/a)) "
                "([rail.example.test](https://rail.example.test/b)). Next."
            ),
            "The corridor ([rail.example.test](https://rail.example.test/a)). Next.",
        )


class NoteTests(unittest.TestCase):
    def test_a_locution_is_no_stop_and_notes_quote_claims(self) -> None:
        turn = travel_claims.Turn(asked=["Gare de Lyon a midi trente"], place="Caen")
        turn.activate()
        turn.collect(
            {
                "link": "https://rail.example.test/caen-paris",
                "text": "Caen to Paris Saint-Lazare: 2 h 07 min, 12 trains a day.",
            }
        )
        answer = (
            "Le train Caen → Paris Saint-Lazare dure 2 h 07 min. "
            "Prenez ensuite le métro ligne 14 jusqu'à Gare de Lyon. "
            "Au-delà, prévoyez une marge."
        )
        checked = turn.check(answer, "fr")
        note = checked.rsplit("\n\n", 1)[-1]
        self.assertIn("“Prenez ensuite le métro ligne 14", note)
        self.assertNotIn("Au-delà", note)
        self.assertIn("Au-delà, prévoyez une marge.", checked)


if __name__ == "__main__":
    unittest.main()
