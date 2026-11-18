"""Trip answers on staging -114 (2026-11-18, seven trips on a phone).

A first sentence left out for an unsourced time took the request it restated
with it; a slow connection and a journey of another day read in snippets were
kept next to the route's usual times; a forum thread was the only source of a
train's running days.
"""

import unittest

from services.orchestrator import redirects, travel_claims


def turn(asked: str, place: str, language: str, text: str) -> travel_claims.Turn:
    found = travel_claims.Turn(
        asked=[asked], place=place, language=language, zone="Europe/Paris"
    )
    found.activate()
    found.collect({"link": "https://rail.example.test/trip", "text": text})
    return found


class OpeningTests(unittest.TestCase):
    def test_a_value_left_out_keeps_the_restated_request(self) -> None:
        checked = turn(
            "Demain j'ai un dejeuner a Gare de Valmont a midi trente, depuis chez moi",
            "Rue des Tilleuls, Brenneville, France",
            "fr",
            "Brenneville - Orlen Gare de Valmont : départ 9h04, arrivée 11h26.",
        ).check(
            "Pour être à l'heure à votre déjeuner à Orlen Gare de Valmont à "
            "12 h 30, deux départs conviennent : 7 h 12 et 8 h 01. Le départ de "
            "9 h 04 vous laisse une bonne marge.",
            "fr",
        )
        opening, body, note = checked.split("\n\n")
        self.assertEqual(
            opening,
            "Pour être à l'heure à votre déjeuner à Orlen Gare de Valmont à 12 h 30 :",
        )
        self.assertEqual(body, "Le départ de 9 h 04 vous laisse une bonne marge.")
        self.assertIn("7 h 12", note)


class NoiseTests(unittest.TestCase):
    def test_a_value_of_another_day_goes_without_a_note(self) -> None:
        found = turn(
            "I need to be at Valmont Central tomorrow by 2pm",
            "Kestrel Street, Orlen",
            "en",
            "Orlen to Valmont: 4h 30m, 4h 45m. 16 Nov 2026: 10h 34m.",
        )
        day = found.trip_dates()[0]
        other = "2020-01-06" if day != "2020-01-06" else "2020-01-07"
        answer = (
            "Journey times are around 4h 30m or 4h 45m. "
            f"For example, on {other}, a train took 10h 34m. Book early."
        )
        self.assertEqual(
            found.check(answer, "en"),
            "Journey times are around 4h 30m or 4h 45m. Book early.",
        )

    def test_a_far_longer_listed_time_leaves_the_list(self) -> None:
        self.assertEqual(
            travel_claims.outliers_left_out(
                "Journey times around 4h 30m, 4h 45m, or 9h 15m as listed."
            ),
            "Journey times around 4h 30m, 4h 45m as listed.",
        )
        for kept in (
            "Direct 4 h, with a change 6 h, night train 9 h 30.",
            "Départs à 6h12, 7h05 ou 21h40.",
        ):
            self.assertEqual(travel_claims.outliers_left_out(kept), kept)


class ForumTests(unittest.TestCase):
    def test_forums_are_no_source_of_a_trip(self) -> None:
        self.assertTrue(redirects.forum("https://www.reddit.com/r/trains/comments/x/"))
        self.assertTrue(redirects.forum("https://forum.example.test/t/1"))
        self.assertTrue(redirects.forum("https://www.example.test/ShowTopic-g1-i2"))
        self.assertFalse(redirects.forum("https://rail.example.test/timetable"))
        self.assertEqual(
            redirects.forum_claims(
                "Take the express ([rail.example.test](https://rail.example.test/a)). "
                "It runs Monday to Friday ([reddit.com](https://www.reddit.com/r/x/)). "
                "The fastest takes 3h30 "
                "([wiki.example.test](https://wiki.example.test/e)) "
                "([reddit.com](https://www.reddit.com/r/y/)).\n"
                "- Arrive by 5:00 AM ([reddit.com](https://www.reddit.com/r/z/)).\n"
                "- Kept."
            ),
            "Take the express ([rail.example.test](https://rail.example.test/a)). "
            "The fastest takes 3h30 "
            "([wiki.example.test](https://wiki.example.test/e)).\n- Kept.",
        )

    def test_forum_rows_come_last_and_are_not_read_as_sources(self) -> None:
        result: dict[str, object] = {
            "results": [
                {"link": "https://www.reddit.com/r/x/", "snippet": "Runs Mon-Fri."},
                {"link": "https://rail.example.test/a", "snippet": "Timetable."},
            ]
        }
        rows = redirects.results(result)["results"]
        assert isinstance(rows, list)
        self.assertEqual(rows[0]["link"], "https://rail.example.test/a")
        found = travel_claims.Turn(asked=["to Valmont"])
        found.activate()
        found.collect(result)
        self.assertNotIn("Runs Mon-Fri.", " ".join(found.evidence.evidence))


if __name__ == "__main__":
    unittest.main()
