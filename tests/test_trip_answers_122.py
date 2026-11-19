"""Trip answers on staging -122 (2026-11-19, seven trips on a phone).

Caen - Paris: every option of a list had an unsourced time and was left out;
the answer still read "voici les options réalistes:" before a piece of
advice. Streets are fictional; source sentences are made up.
"""

import unittest

from services.orchestrator import travel_claims


def turn(asked: str, place: str, language: str, text: str) -> travel_claims.Turn:
    found = travel_claims.Turn(asked=[asked], place=place, language=language)
    found.activate()
    found.collect({"link": "https://rail.example.test/trip", "text": text})
    return found


class AnnouncementTests(unittest.TestCase):
    def test_a_list_announcement_goes_with_its_list(self) -> None:
        checked = turn(
            "demain j ai un dejeuner a paris gare de lyon a midi et demi, quels trains le matin",
            "Rue des Tilleuls, Brenneville, France",
            "fr",
            "Le trajet le plus rapide dure 1h54.",
        ).check(
            "Pour être à Paris demain avant 12 h 30, voici les options réalistes :\n\n"
            "- départ à 7 h 02, arrivée à 9 h 05 ;\n"
            "- départ à 8 h 10, arrivée à 10 h 12.\n\n"
            "Vérifiez le départ exact la veille sur le site de l'opérateur.",
            "fr",
        )
        self.assertNotIn("voici les options", checked)
        self.assertNotIn("7 h 02", checked.split("\n\n")[0])
        self.assertIn("Vérifiez le départ exact", checked)

    def test_an_announcement_whose_list_stays_keeps_it(self) -> None:
        answer = (
            "Here are the steps:\n\n"
            "- walk to Brenneville station;\n"
            "- take the train to Paris."
        )
        checked = turn(
            "how do i get to paris tomorow",
            "Rue des Tilleuls, Brenneville, France",
            "en",
            "Trains run from Brenneville station to Paris.",
        ).check(answer, "en")
        self.assertIn("Here are the steps:", checked)


if __name__ == "__main__":
    unittest.main()
