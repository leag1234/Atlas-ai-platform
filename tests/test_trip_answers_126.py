"""Trip answers on staging -126 (2026-11-19, seven trips on a phone).

Munich - Berlin: the unsourced "4h to 4h30m" went, "depart no later than
about 09:30" worked out from it for 14:00 stayed. Villeurbanne - Geneva
airport: "metro A towards Vaulx-en-Velin La Soie" was kept from a page naming
the line's two termini, although the leg ran the other way. Madrid -
Barcelona by 13:00: a train arriving at 16:44 was offered without a word.
Streets are fictional; source sentences are made up.
"""

import unittest

from services.orchestrator import travel_claims, travel_rebuild

RAIL = "https://rail.example.test/trip"
BOARD = "https://station.example.test/departures"
METRO = "https://metro.example.test/line-a"

BERLIN_ASKED = (
    "I need to be at Berlin Hauptbahnhof tomorrow by 2pm, how do I get there "
    "from here by train?"
)
BERLIN_PLACE = "Lindenweg 4, 80634 Munich, Germany"
LYON_ASKED = (
    "je dois etre a l aeroport de geneve demain a 9h, comment j y vais depuis "
    "chez moi en transport en commun ?"
)
LYON_PLACE = "Rue des Glycines, 69100 Villeurbanne, France"
MADRID_ASKED = (
    "mañana tengo que estar en barcelona sants a las 13h, que tren cojo desde aqui?"
)
MADRID_PLACE = "Calle del Olmo 3, 28012 Madrid, Spain"
LINE_A = (
    "Metro ligne A : Perrache, Ampère Victor Hugo, Bellecour, Cordeliers, "
    "Hôtel de Ville, Foch, Masséna, Charpennes, République Villeurbanne, "
    "Gratte-Ciel, Flachet, Cusset, Laurent Bonnevay, Vaulx-en-Velin La Soie."
)


def turn(
    asked: str, place: str, language: str, *pages: tuple[str, str]
) -> travel_claims.Turn:
    found = travel_claims.Turn(asked=[asked], language=language)
    found.located(place=place)
    for url, text in pages:
        found.collect({"link": url, "text": text})
    return found


def berlin(*pages: tuple[str, str]) -> travel_claims.Turn:
    return turn(
        BERLIN_ASKED,
        BERLIN_PLACE,
        "en",
        (RAIL, "Direct ICE trains run from Munich Hbf to Berlin Hbf, 27 a day."),
        *pages,
    )


class WorkedOutTimeTests(unittest.TestCase):
    LEG = (
        "From **Munich Hbf**, take a direct **ICE** to **Berlin Hbf** "
        f"([rail]({RAIL}))."
    )
    LATEST = (
        "To arrive at **Berlin Hbf by 14:00**, depart Munich Hbf no later than "
        "about **09:30**."
    )

    def test_a_time_worked_out_from_a_removed_duration_goes(self) -> None:
        # A page gives an unrelated 09:30: no source of the latest departure.
        checked = berlin(
            (BOARD, "Departures Munich Hbf: 09:30 RE 4012 to Nuremberg.")
        ).check(f"{self.LEG} The ride takes 4h to 4h30m.\n\n{self.LATEST}", "en")
        self.assertNotIn("09:30", checked.split("Not verified")[0])
        self.assertNotIn("4h30m", checked.split("Not verified")[0])
        self.assertIn("take a direct **ICE**", checked)
        self.assertIn("4h to 4h30m, 09:30", checked)

    def test_the_deadline_does_not_source_the_duration(self) -> None:
        # Same paragraph: 14:00 - 09:30 = 4 h 30 is no sourced arithmetic.
        checked = berlin((BOARD, "Departures Munich Hbf: 09:30 RE 4012.")).check(
            f"{self.LEG} The ride takes 4h to 4h30m. {self.LATEST}", "en"
        )
        self.assertNotIn("about **09:30**", checked)
        self.assertIn("4h to 4h30m", checked)

    def test_a_rewrite_keeping_only_the_worked_out_time_loses_it(self) -> None:
        checked = berlin((BOARD, "Departures Munich Hbf: 09:30 RE 4012."))
        checked.findings(f"{self.LEG} The ride takes 4h to 4h30m.")
        rewrite = checked.check(f"{self.LEG}\n\n{self.LATEST}", "en")
        self.assertNotIn("about **09:30**", rewrite)
        self.assertIn("take a direct **ICE**", rewrite)

    def test_a_time_from_a_sourced_duration_stays(self) -> None:
        answer = (
            f"{self.LEG} The ride takes 4 h 30 min ([rail]({RAIL})).\n\n{self.LATEST}"
        )
        checked = berlin(
            (RAIL, "The ICE from Munich Hbf to Berlin Hbf takes 4 h 30 min.")
        )
        self.assertEqual(checked.check(answer, "en"), answer)

    def test_a_timetable_time_with_its_arrival_stays(self) -> None:
        answer = (
            f"{self.LEG}\n\nThe ICE 509 leaves at 09:30 and arrives at 13:58 "
            f"([board]({BOARD})), in time for 14:00. Allow 4h to 4h30m in all."
        )
        checked = berlin((BOARD, "ICE 509: Munich Hbf 09:30, Berlin Hbf 13:58.")).check(
            answer, "en"
        )
        self.assertIn("leaves at 09:30 and arrives at 13:58", checked)
        self.assertNotIn("Allow 4h to 4h30m", checked.split("Not verified")[0])


class DeadlineTests(unittest.TestCase):
    def test_the_time_to_arrive_by_in_five_languages(self) -> None:
        cases = {
            BERLIN_ASKED: 840,
            LYON_ASKED: 540,
            MADRID_ASKED: 780,
            "need to get to kyoto station tomorow before 10 am by shinkansen": 600,
            "how do i get to sydney airport from here tomorow morning, flight at 8am": 480,
            "Demain j'ai un dejeuner a Gare de Lyon a midi trente, horaires ?": 750,
            "Ich muss morgen bis 14 Uhr in Hamburg sein, welcher Zug?": 840,
            "domani devo essere a Milano Centrale entro le 11:30, che treno prendo": 690,
            "I can leave after 8, I have to be in Leeds by 12:15": 735,
        }
        for asked, minutes in cases.items():
            with self.subTest(asked=asked):
                self.assertEqual(travel_claims.arrival_deadline(asked), minutes)

    def test_no_time_to_arrive_by(self) -> None:
        for asked in (
            "I want to leave at 8am, how do I get to Leeds?",
            "quiero salir a las 9, como voy a Valencia",
            "how do I get to the airport tomorrow?",
            "train to Lyon at 7 or at 9, which one is faster to be there?",
        ):
            with self.subTest(asked=asked):
                self.assertIsNone(travel_claims.arrival_deadline(asked))

    def test_the_last_message_with_a_time_sets_it(self) -> None:
        found = travel_claims.Turn(
            asked=["I need to be in Leeds by 12:00", "actually by 11:00 please"]
        )
        self.assertEqual(found.deadline(), 660)
        found = travel_claims.Turn(asked=["I need to be in Leeds by 12:00", "thanks"])
        self.assertEqual(found.deadline(), 720)


def madrid(text: str) -> travel_claims.Turn:
    return turn(MADRID_ASKED, MADRID_PLACE, "es", (RAIL, text))


SPANISH_TIMES = (
    "AVE 03063 sale de Madrid Atocha a las 5:50 h y llega a Barcelona a las "
    "8:55 h. Tren 6123: salida 13:42 de Madrid, llegada 16:44 a Barcelona, "
    "duración 3 horas y 2 minutos."
)


class LateDepartureTests(unittest.TestCase):
    EARLY = (
        "Por ejemplo, un tren AVE sale de Madrid Atocha a las 5:50 h y llega a "
        f"Barcelona a las 8:55 h ([rail]({RAIL}))."
    )
    LATE = (
        "También hay opciones como un tren que sale a las 13:42 de Madrid y "
        "llega a Barcelona a las 16:44, con una duración de 3 horas y 2 "
        f"minutos ([rail]({RAIL}))."
    )

    def test_a_departure_arriving_after_the_deadline_goes(self) -> None:
        checked = madrid(SPANISH_TIMES).check(f"{self.EARLY} {self.LATE}", "es")
        self.assertIn("5:50 h y llega a Barcelona a las 8:55 h", checked)
        self.assertNotIn("También hay opciones", checked)
        self.assertTrue(
            checked.endswith(
                "Se omite porque llega después de las 13:00: 13:42 → 16:44."
            )
        )

    def test_departures_in_time_stay(self) -> None:
        self.assertEqual(madrid(SPANISH_TIMES).check(self.EARLY, "es"), self.EARLY)

    def test_an_arrival_at_the_deadline_is_in_time(self) -> None:
        answer = (
            "El tren 6101 sale de Madrid a las 10:00 y llega a Barcelona a las "
            f"13:00 ([rail]({RAIL}))."
        )
        text = "Tren 6101: salida 10:00 de Madrid, llegada 13:00 a Barcelona."
        self.assertEqual(madrid(text).check(answer, "es"), answer)

    def test_an_answer_saying_it_is_too_late_keeps_it(self) -> None:
        answer = (
            "El siguiente sale a las 13:42 y llega a las 16:44, demasiado tarde "
            f"para las 13:00 ([rail]({RAIL}))."
        )
        self.assertEqual(madrid(SPANISH_TIMES).check(answer, "es"), answer)

    def test_a_late_row_of_a_timetable_goes(self) -> None:
        answer = (
            "| Tren | Salida | Llegada |\n"
            "|---|---|---|\n"
            "| AVE 03063 | 5:50 | 8:55 |\n"
            "| Tren 6123 | 13:42 | 16:44 |\n\n"
            f"Horarios: [rail]({RAIL})."
        )
        checked = madrid("AVE 03063 5:50 8:55. Tren 6123 13:42 16:44.").check(
            answer, "es"
        )
        self.assertIn("| AVE 03063 | 5:50 | 8:55 |", checked)
        self.assertNotIn("6123", checked.split("Se omite")[0])
        self.assertIn("13:42 → 16:44", checked)

    def test_a_departure_and_its_duration_give_the_arrival(self) -> None:
        answer = (
            f"{self.EARLY} El tren 6123 sale a las 13:42 de Madrid, con una "
            f"duración de 3 horas y 2 minutos ([rail]({RAIL}))."
        )
        checked = madrid(SPANISH_TIMES).check(answer, "es")
        self.assertNotIn("El tren 6123", checked)
        self.assertIn("13:42 → 16:44", checked)

    def test_no_deadline_no_check(self) -> None:
        found = turn(
            "quiero salir mañana despues de las 13h hacia barcelona",
            MADRID_PLACE,
            "es",
            (RAIL, SPANISH_TIMES),
        )
        answer = f"{self.EARLY} {self.LATE}"
        self.assertEqual(found.check(answer, "es"), answer)

    def test_the_rewrite_is_told_which_departures_arrive_too_late(self) -> None:
        found = madrid(SPANISH_TIMES)
        findings = found.findings(f"{self.EARLY} {self.LATE}")
        prompt = travel_rebuild._prompt(found, MADRID_ASKED, "", findings, "es", [])
        self.assertIn("ARRIVE BY: 13:00", prompt)
        self.assertIn("13:42 → 16:44", prompt)


def lyon(*texts: str) -> travel_claims.Turn:
    return turn(LYON_ASKED, LYON_PLACE, "fr", *((METRO, text) for text in texts))


class DirectionTests(unittest.TestCase):
    def test_a_direction_no_page_gives_for_the_leg_goes(self) -> None:
        answer = (
            "1. **Métro ligne A** depuis la station **Gratte-Ciel** en direction "
            f"de **Vaulx-en-Velin La Soie** ([TCL]({METRO})).\n"
            "2. **Train** depuis **Gare de Lyon-Part-Dieu** vers Genève."
        )
        checked = lyon(
            "La ligne A du métro relie Perrache à Vaulx-en-Velin La Soie. Elle "
            "dessert notamment Charpennes, Gratte-Ciel et Laurent Bonnevay."
        ).check(answer, "fr")
        self.assertIn(
            f"**Métro ligne A** depuis la station **Gratte-Ciel** ([TCL]({METRO})).",
            checked,
        )
        self.assertNotIn("Vaulx", checked.split("Non vérifié")[0])
        self.assertIn("“en direction de Vaulx-en-Velin La Soie”", checked)

    def test_the_stop_list_gives_the_direction_of_the_leg(self) -> None:
        # From Gratte-Ciel to Charpennes, line A runs towards Perrache.
        right = (
            "Depuis Gratte-Ciel, prenez la ligne A direction Perrache jusqu'à "
            "Charpennes."
        )
        self.assertEqual(lyon(LINE_A).check(right, "fr"), right)
        wrong = right.replace("Perrache", "Vaulx-en-Velin La Soie")
        checked = lyon(LINE_A).check(wrong, "fr")
        self.assertTrue(
            checked.startswith(
                "Depuis Gratte-Ciel, prenez la ligne A jusqu'à Charpennes."
            )
        )
        self.assertIn("“direction Vaulx-en-Velin La Soie”", checked)

    def test_the_next_step_gives_the_stop_to_get_off(self) -> None:
        answer = (
            "Prenez la ligne A à Gratte-Ciel, direction Perrache. Descendez à "
            "Charpennes."
        )
        self.assertEqual(lyon(LINE_A).check(answer, "fr"), answer)
        # The next step leaves the line: the page gives no direction for it.
        answer = (
            "Prenez la ligne A à Gratte-Ciel, direction Vaulx-en-Velin La Soie. "
            "Le train part de Lyon Part-Dieu."
        )
        checked = lyon(LINE_A).check(answer, "fr")
        self.assertIn("Prenez la ligne A à Gratte-Ciel.", checked)

    def test_a_page_stating_the_direction_holds_it(self) -> None:
        answer = "Take metro line A at Gratte-Ciel towards Perrache."
        checked = turn(
            "how do I get to Lyon Part-Dieu by 9am",
            LYON_PLACE,
            "en",
            (METRO, "At Gratte-Ciel, metro line A towards Perrache every 3 minutes."),
        ).check(answer, "en")
        self.assertEqual(checked, answer)

    def test_a_direction_in_parentheses_goes_alone(self) -> None:
        answer = (
            "Depuis Gratte-Ciel, prenez le métro (ligne A, direction Vaulx-en-Velin "
            "La Soie) jusqu'à Charpennes."
        )
        checked = lyon(LINE_A).check(answer, "fr")
        self.assertTrue(
            checked.startswith(
                "Depuis Gratte-Ciel, prenez le métro (ligne A) jusqu'à Charpennes."
            )
        )


if __name__ == "__main__":
    unittest.main()
