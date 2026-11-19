"""Trip answers on staging -113 (2026-11-18, seven trips on a phone).

A first sentence restating the request ("To be at Geneva Airport on Thursday
at 9:00 from Gratte-Ciel, take ...") was left out whole for its unverified
leg: the answer opened mid-thought ("From there, ...") and the note quoted the
restatement instead of the leg.

The restatement says "demain", never a written date: a date written in the
test becomes another day than the trip once that day has come (CI of
2026-11-19 00:00 UTC).
"""

import unittest

from services.orchestrator import travel_claims


def turn(asked: str, place: str, language: str, text: str) -> travel_claims.Turn:
    found = travel_claims.Turn(asked=[asked], place=place, language=language)
    found.activate()
    found.collect({"link": "https://rail.example.test/trip", "text": text})
    return found


class OpeningTests(unittest.TestCase):
    def test_the_request_restated_still_opens_the_answer(self) -> None:
        checked = turn(
            "je dois etre a l'aeroport de Valmorel demain a 9h depuis chez moi",
            "Rue des Tilleuls, Brenneville, France",
            "fr",
            "Orlen Centrale - Valmorel : départ 6h38, arrivée 8h43. Les trains "
            "pour Valmorel partent d'Orlen Centrale.",
        ).check(
            "Pour être à l'aéroport de Valmorel demain à 9h00 "
            "depuis Brenneville, prenez le métro A jusqu'à Saint-Arnoult puis le "
            "métro B jusqu'à Orlen Centrale. De là, les trains pour Valmorel "
            "partent d'Orlen Centrale. Un départ à 6h38 arrive à 8h43.",
            "fr",
        )
        opening, body, note = checked.split("\n\n")
        self.assertEqual(
            opening,
            "Pour être à l'aéroport de Valmorel demain à 9h00 depuis Brenneville :",
        )
        self.assertTrue(body.startswith("Les trains pour Valmorel partent"))
        self.assertIn("“prenez le métro A jusqu'à Saint-Arnoult", note)
        self.assertNotIn("Pour être", note)

    def test_a_clause_stating_a_route_is_no_opening(self) -> None:
        checked = turn(
            "I need to be at Valmorel Airport tomorrow by 9am",
            "Kestrel Street, Orlen",
            "en",
            "Orlen Central to Valmorel Airport: 6:38 departure, 8:43 arrival.",
        ).check(
            "Take line 7 from Brannock Square, then the shuttle to Valmorel "
            "Airport. The 6:38 departure arrives at 8:43.",
            "en",
        )
        self.assertTrue(checked.startswith("The 6:38 departure arrives at 8:43."))


if __name__ == "__main__":
    unittest.main()
