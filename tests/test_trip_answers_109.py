"""Trip answers on staging -109 (2026-11-17, Madrid-Barcelona through the API).

Two links in one parenthesis were wrapped again ("(([a](...)), ([b](...)))"),
and the note listed the trip's own stations ("Barcelona Sants – Madrid Puerta
de Atocha – Almudena Grandes – Cercanías") as the unverified claim instead of
the connection the removed sentence stated.
"""

import unittest

from services.orchestrator import route_facts, travel_claims, travel_rebuild

PAGE = "https://rail.example.test/rutas/madrid-barcelona"


class GroupTests(unittest.TestCase):
    def test_a_group_of_links_is_wrapped_once(self) -> None:
        text = (
            "Unas 2 h 30 min (([renfe.example.test](https://renfe.example.test/a)), "
            "([ouigo.example.test](https://ouigo.example.test/b)))."
        )
        self.assertEqual(
            travel_rebuild.unwrapped(text),
            "Unas 2 h 30 min ([renfe.example.test](https://renfe.example.test/a)), "
            "([ouigo.example.test](https://ouigo.example.test/b)).",
        )


class NoteTests(unittest.TestCase):
    def test_the_note_quotes_the_connection_not_the_stations(self) -> None:
        turn = travel_claims.Turn(
            asked=["mañana a Barcelona Sants a las 13h desde Sol"]
        )
        turn.activate()
        turn.collect(
            {
                "link": PAGE,
                "snippet": "Estación Madrid Puerta de Atocha - Almudena Grandes. "
                + "Información del servicio. " * 30
                + "Estación Barcelona Sants.",
            }
        )
        answer = (
            "Toma el Cercanías en Sol hasta Madrid Puerta de Atocha - Almudena Grandes "
            "y después el AVE a Barcelona Sants."
        )
        labels = turn.findings(answer).labels()
        self.assertEqual(len(labels), 1, labels)
        self.assertTrue(labels[0].startswith("“Toma el Cercanías en Sol"), labels)

    def test_quotes_are_short_and_plain(self) -> None:
        quoted = route_facts.quoted(
            "**Toma** el [Cercanías](https://x.example.test) desde Sol hasta Atocha, "
            "luego el AVE de las 7 hasta Barcelona Sants pasando por Zaragoza y Lleida"
        )
        self.assertTrue(quoted.startswith("“Toma el Cercanías desde Sol"), quoted)
        self.assertNotIn("**", quoted)
        self.assertNotIn("https", quoted)
        self.assertTrue(quoted.endswith("…”"))
        self.assertLessEqual(len(quoted), 95)


if __name__ == "__main__":
    unittest.main()
