"""Trip answers on staging -104 (2026-11-17, a phone battery of seven trips).

A time cut short ("arrivée ~12h...") stayed in an answer, links were wrapped
twice in parentheses, the stations of a line listed on the line's own page
were "not verified" because they sat far below its name, and a change of
station the page gave next to the times was not said. Streets are fictional.
"""

import unittest

from services.orchestrator import travel_claims, travel_rebuild

PAGE = "https://rail.example.test/caen-gare-de-lyon"
LINE = "https://wiki.example.test/T8_Airport_Line"


def trip(snippet: str, asked: str, url: str = PAGE) -> travel_claims.Turn:
    turn = travel_claims.Turn(asked=[asked])
    turn.located(place="Rue des Glycines, 14000 Caen, France")
    turn.retrieved = lambda: [url]
    turn.collect({"link": url, "title": "Timetable", "snippet": snippet})
    return turn


class TruncatedValueTests(unittest.TestCase):
    def test_a_time_cut_short_is_never_kept(self) -> None:
        turn = trip("Caen 10:05 → Paris 12:31.", "Paris demain midi trente")
        for answer in ("Départ 10h05, arrivée ~12h...", "Départ 10h05, arrivée 12:…"):
            with self.subTest(answer=answer):
                found = turn.findings(answer)
                self.assertTrue(found.values, answer)
                self.assertNotIn("12h", turn.kept(answer, found))

    def test_an_open_ended_duration_is_a_claim(self) -> None:
        turn = trip("Acela New York 06:00 → Boston 09:30.", "Boston tomorrow 11am")
        self.assertTrue(turn.findings("Regional trains take roughly 4+ hours."))


class LinkTests(unittest.TestCase):
    def test_links_are_wrapped_once(self) -> None:
        text = (
            "Take the Acela (([amtrak.example.test](https://amtrak.example.test/a)))."
        )
        self.assertEqual(
            travel_rebuild.unwrapped(text),
            "Take the Acela ([amtrak.example.test](https://amtrak.example.test/a)).",
        )
        chosen = [travel_rebuild.Excerpt("https://amtrak.example.test/a", "x")]
        self.assertEqual(
            travel_rebuild._linked(
                "Take it ([1](https://amtrak.example.test/a)).", chosen
            ),
            "Take it ([amtrak.example.test](https://amtrak.example.test/a)).",
        )
        self.assertEqual(
            travel_rebuild._linked("Take it ([1]).", chosen),
            "Take it ([amtrak.example.test](https://amtrak.example.test/a)).",
        )


class LinePageTests(unittest.TestCase):
    def test_a_lines_page_lists_its_stops_anywhere(self) -> None:
        page = (
            "The T8 Airport & South Line is a suburban rail service. "
            + "History and rolling stock. " * 40
            + "Stations: Central, Green Square, Mascot, Domestic Airport, "
            "International Airport, Wolli Creek."
        )
        turn = trip(page, "sydney airport from here tomorow", LINE)
        answer = "Take the T8 to Domestic Airport or International Airport station."
        self.assertFalse(turn.findings(answer).route.spans)
        other = "Take the T8 to Bondi Junction station."
        self.assertTrue(turn.findings(other).route.spans)


class ConnectionTests(unittest.TestCase):
    SOURCE = (
        "Caen 08:26 → Paris Saint-Lazare 10:20, correspondance à Paris Saint-Lazare, "
        "arrivée Paris Gare de Lyon 10:54."
    )

    def test_a_change_of_station_the_page_gives_is_said(self) -> None:
        turn = trip(self.SOURCE, "déjeuner à Gare de Lyon demain midi trente")
        answer = "Départ de Caen à 8h26, arrivée à Gare de Lyon à 10h54."
        note = travel_rebuild.connection_note(turn, answer, "fr")
        self.assertIn("correspondance à Paris Saint-Lazare", note)
        stated = answer + " Changement à Paris Saint-Lazare."
        self.assertEqual(travel_rebuild.connection_note(turn, stated, "fr"), "")

    def test_a_direct_train_needs_no_note(self) -> None:
        turn = trip(
            "Caen 08:05 → Paris Saint-Lazare 10:12, direct, sans correspondance.",
            "Paris demain",
        )
        answer = "Départ 8h05, arrivée à Paris Saint-Lazare à 10h12."
        self.assertEqual(travel_rebuild.connection_note(turn, answer, "fr"), "")


if __name__ == "__main__":
    unittest.main()
