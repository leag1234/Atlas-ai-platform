"""Trip answers on staging -106 (2026-11-17, Madrid-Barcelona through the API).

The answer linked a Madrid-Sevilla timetable as the source of a Madrid-
Barcelona train, and listed "Madrid Puerta de Atocha" as not verified although
the timetable read wrote "Madrid Atocha". Streets are fictional.
"""

import unittest

from services.orchestrator import travel_claims, travel_rebuild

PAIR = "https://rail.example.test/horarios/ave/madrid-barcelona/"
OTHER = "https://rail.example.test/horarios/ave/madrid-sevilla/"
QUESTION = "mañana tengo que estar en barcelona sants a las 13h, que tren cojo desde la puerta del sol en madrid?"


def trip() -> travel_claims.Turn:
    turn = travel_claims.Turn(asked=[QUESTION])
    turn.located(place="la puerta del sol en madrid")
    turn.retrieved = lambda: [PAIR, OTHER]
    turn.collect(
        {
            "link": PAIR,
            "title": "Horarios AVE Madrid Barcelona",
            "snippet": "Estación origen: Madrid Atocha 07:00. Estación destino: "
            "Barcelona Sants 09:30. AVE 03063.",
        }
    )
    turn.collect(
        {
            "link": OTHER,
            "title": "Horarios AVE Madrid Sevilla",
            "snippet": "Estación origen: Madrid Atocha 07:00. Estación destino: "
            "Sevilla Santa Justa 09:38.",
        }
    )
    return turn


class OffRouteTests(unittest.TestCase):
    def test_a_page_about_another_trip_is_no_source(self) -> None:
        turn = trip()
        answer = (
            "Coge el AVE de las 07:00 en Madrid Atocha, llega a Barcelona Sants a "
            f"las 09:30 ([rail.example.test]({OTHER})). Horarios: [aquí]({PAIR})."
        )
        self.assertEqual(travel_rebuild.off_route(turn, QUESTION, answer), {OTHER})
        cleaned = travel_rebuild._without(answer, {OTHER})
        self.assertNotIn(OTHER, cleaned)
        self.assertIn(f"[aquí]({PAIR})", cleaned)
        self.assertIn("a las 09:30.", cleaned)
        self.assertTrue(
            all(e.url != OTHER for e in travel_rebuild.excerpts(turn, QUESTION))
        )

    def test_a_city_the_answer_names_keeps_its_page(self) -> None:
        turn = trip()
        answer = "Desde Madrid también hay AVE a Sevilla."
        self.assertEqual(travel_rebuild.off_route(turn, QUESTION, answer), set())

    def test_no_judgement_without_a_page_of_this_trip(self) -> None:
        turn = travel_claims.Turn(asked=[QUESTION])
        turn.activate()
        turn.collect({"link": OTHER, "snippet": "Madrid Atocha 07:00 Sevilla 09:38."})
        self.assertEqual(travel_rebuild.off_route(turn, QUESTION, "AVE 07:00."), set())


class StationNameTests(unittest.TestCase):
    def test_a_long_station_name_matches_its_short_form(self) -> None:
        turn = trip()
        answer = "El AVE sale de Madrid Puerta de Atocha a las 07:00 y llega a Barcelona Sants a las 09:30."
        found = turn.findings(answer)
        self.assertFalse(found.route.spans, found.labels())

    def test_the_last_word_of_a_name_is_still_needed(self) -> None:
        turn = trip()
        answer = "El AVE sale de Madrid Puerta de Chamartín a las 07:00 y llega a Barcelona Sants a las 09:30."
        self.assertTrue(turn.findings(answer).route.spans)


if __name__ == "__main__":
    unittest.main()
