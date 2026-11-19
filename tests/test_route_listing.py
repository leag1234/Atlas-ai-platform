"""A network page names every line: words close together there prove no
route (staging -122, Lyon: "Correspondance a Perrache, puis metro B" was kept
from a page listing lines A, B and D; line B never reaches Perrache).
Stops and lines are fictional.
"""

import unittest

from services.orchestrator import travel_claims

NETWORK = (
    "Le reseau compte trois lignes. Ligne A : Pont Neuf, Belvue, Carrefour, "
    "Grand-Tilleul. Ligne B : Carrefour, Gare Centrale, Gare des Saules. "
    "Ligne D : Gare du Nord, Halles, Gare des Saules."
)


def turn(*texts: str) -> travel_claims.Turn:
    found = travel_claims.Turn(
        asked=["je vais a la gare des saules demain matin, comment j y vais"],
        place="Rue des Lilas, Brenneville, France",
        language="fr",
    )
    found.activate()
    for text in texts:
        found.collect({"link": "https://metro.example.test/plan", "text": text})
    return found


class ListingTests(unittest.TestCase):
    def test_a_line_at_a_stop_it_never_serves_goes(self) -> None:
        answer = "Prenez le métro B à Pont Neuf jusqu'à Gare des Saules. Bon voyage."
        checked = turn(NETWORK).check(answer, "fr")
        self.assertNotIn("métro B à Pont Neuf", checked.split("\n\n")[0])

    def test_a_transfer_the_page_gives_stays(self) -> None:
        answer = (
            "Prenez le métro A de Grand-Tilleul à Carrefour, puis le métro B "
            "jusqu'à Gare des Saules."
        )
        self.assertEqual(turn(NETWORK).check(answer, "fr"), answer)

    def test_a_page_about_one_line_keeps_the_window_rule(self) -> None:
        page = "La ligne B relie Carrefour a Gare des Saules en 12 minutes."
        answer = "Le métro B va de Carrefour à Gare des Saules."
        self.assertEqual(turn(page).check(answer, "fr"), answer)


if __name__ == "__main__":
    unittest.main()
