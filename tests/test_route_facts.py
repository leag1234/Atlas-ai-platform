"""Route facts in trip answers: lines, stops, directions and "direct" are sourced.

Staging -98 (2026-11-16, phone, device position, no routing engine): three
trips kept invented route facts because the travel check read only numbers. A
metro transfer at a stop the second line does not serve, a train said to stop
under the airport terminal when its source named the city only, an S-Bahn leg
"from general knowledge" under "The plan, in three legs:" with one leg left.

Station and line names are the public ones of the reported trips; every
address and person below is fictional. Answers are in French, English and
German, written the way the model wrote them.
"""

import unittest

from services.orchestrator import journey, route_facts, travel_claims, unsourced_details

HOME = "8 allée des Platanes Fictifs, 69100 Villeurbanne"
LINE_A = (
    "Métro ligne A : Perrache, Ampère Victor Hugo, Bellecour, Cordeliers, "
    "Hôtel de Ville, Foch, Masséna, Charpennes, République Villeurbanne, "
    "Gratte-Ciel, Flachet, Cusset, Laurent Bonnevay, Vaulx-en-Velin La Soie."
)
LINE_B = (
    "Ligne B du métro : Charpennes, Brotteaux, Gare Part-Dieu, Place Guichard, "
    "Saxe-Gambetta, Jean Macé, Place Jean Jaurès, Debourg, Stade de Gerland, "
    "Gare d'Oullins."
)
TRAIN = "TER 96512 Lyon Part-Dieu 06:38 → Genève 08:43, direct, durée 2 h 05."
S8 = (
    "S8: Herrsching – Pasing – Hauptbahnhof – Marienplatz – Ostbahnhof – "
    "Ismaning – Flughafen München. Departures every 20 minutes."
)


def turn_with(*sources: str, asked: str = "", place: str = "") -> travel_claims.Turn:
    turn = travel_claims.Turn(asked=[asked] if asked else [])
    turn.activate()
    if place:
        turn.located(place=place)
    for text in sources:
        turn.collect({"title": "", "snippet": text})
    return turn


def body_and_note(checked: str) -> tuple[str, str]:
    body, _, note = checked.rpartition("\n\n")
    return body, note


class OwnerDefectTests(unittest.TestCase):
    def test_transfer_at_a_stop_the_second_line_does_not_serve(self) -> None:
        answer = (
            "Depuis la station Gratte-Ciel (métro ligne A, direction Perrache), "
            "descendez jusqu'à Bellecour, puis prenez la ligne B direction Gare "
            "Part-Dieu. Bon voyage."
        )
        turn = turn_with(
            LINE_A, LINE_B, asked="je dois être à l'aéroport de genève demain 9h"
        )
        body, note = body_and_note(turn.check(answer, "fr"))
        self.assertEqual(body, "Bon voyage.")
        self.assertIn("ligne B", note)
        self.assertIn("Bellecour", note)
        self.assertIn("calculateur d’itinéraire de l’exploitant", note)
        # Line A serves both stops, line B serves both: the right path holds,
        # each line on its own page, the transfer stop on both.
        right = (
            "Depuis Gratte-Ciel, prenez la ligne A direction Perrache jusqu'à "
            "Charpennes, puis la ligne B direction Gare d'Oullins jusqu'à Gare "
            "Part-Dieu."
        )
        self.assertEqual(turn.check(right, "fr"), right)

    def test_a_train_to_the_city_does_not_stop_at_the_airport(self) -> None:
        answer = (
            "Prenez un train direct Lyon Part-Dieu 6 h 38 → Genève 8 h 43. Il vous "
            "dépose directement à la gare Genève Aéroport, située sous le terminal."
            "\n\nBon voyage."
        )
        turn = turn_with(
            TRAIN,
            "Aéroport de Genève : parkings, accès et plans des terminaux.",
            asked="Je dois être à l'aéroport de Genève demain à 9h",
        )
        body, note = body_and_note(turn.check(answer, "fr"))
        self.assertEqual(
            body,
            "Prenez un train direct Lyon Part-Dieu 6 h 38 → Genève 8 h 43.\n\n"
            "Bon voyage.",
        )
        self.assertIn("Genève Aéroport", note)
        # The source that names the airport station with this train holds it.
        sourced = turn_with(TRAIN.replace("Genève 08:43", "Genève Aéroport 08:43"))
        self.assertEqual(sourced.check(answer, "fr"), answer)

    def test_plan_keeps_no_text_about_removed_legs(self) -> None:
        answer = (
            "The plan, in three legs:\n\n"
            "1. Walk from your position to Marienplatz station (about 6 min).\n"
            "2. Take the S8 towards Munich Airport from Marienplatz.\n"
            "3. At the airport, follow the signs to Terminal 2.\n\n"
            "Trains run every few minutes during the morning. The S-Bahn leg "
            "detail is from general knowledge, so check the MVV journey planner."
        )
        turn = turn_with(
            "Munich Airport Terminal 2: check-in and departures",
            asked="I need to be at Munich airport by 8am",
            place="Fictional Lane 4, 80331 Munich",
        )
        body, note = body_and_note(turn.check(answer, "en"))
        self.assertEqual(
            body,
            "The plan:\n\n1. At the airport, follow the signs to Terminal 2.",
        )
        for value in ("S8", "Marienplatz", "every few minutes"):
            self.assertIn(value, note)
        self.assertIn("operator’s journey planner", note)

    def test_sourced_legs_stay_in_german(self) -> None:
        answer = (
            "Nehmen Sie am Marienplatz die S8 Richtung Flughafen München. "
            "Steigen Sie am Ostbahnhof in die U5 um. Gute Reise!"
        )
        turn = turn_with(S8, asked="Ich muss um 8 Uhr am Flughafen sein")
        body, note = body_and_note(turn.check(answer, "de"))
        self.assertEqual(
            body,
            "Nehmen Sie am Marienplatz die S8 Richtung Flughafen München. Gute Reise!",
        )
        # Notes quote the claim, never a word cut from it (staging -112).
        self.assertTrue(
            note.startswith(
                "Nicht überprüft und daher weggelassen: "
                "“Steigen Sie am Ostbahnhof in die U5”"
            )
        )
        self.assertIn("Fahrplanauskunft", note)

    def test_a_range_is_read_whole_wherever_a_value_appears(self) -> None:
        # Staging -98: "20–25 min" was kept in one answer, removed in another:
        # only its upper bound was read, and a source gave "25 min" for
        # another trip.
        answer = "Comptez environ 20–25 min (métro ligne 14 direct)."
        unrelated = turn_with("Le RER A relie Châtelet à Nation en 25 min environ.")
        checked = unrelated.check(answer, "fr")
        self.assertTrue(checked.startswith("Non vérifié, donc retiré : 20–25 min"))
        sourced = turn_with(
            "Métro ligne 14, direct : comptez 20 à 25 min jusqu'à Gare de Lyon."
        )
        self.assertEqual(sourced.check(answer, "fr"), answer)

    def test_a_parenthesis_alone_goes_when_the_sentence_holds(self) -> None:
        answer = "Allez à Charpennes (ligne B direction Gare d'Oullins), puis marchez."
        turn = turn_with(LINE_A)
        body, _ = body_and_note(turn.check(answer, "fr"))
        self.assertEqual(body, "Allez à Charpennes, puis marchez.")


class RouteTableTests(unittest.TestCase):
    def test_rows_without_a_source_go_and_sourced_rows_stay(self) -> None:
        answer = (
            "| Line | From → to | Departure |\n|---|---|---|\n"
            "| S8 | Marienplatz → Flughafen München | 07:12 |\n"
            "| U5 | Ostbahnhof → Laimer Platz | 07:30 |\n\nHave a good trip."
        )
        turn = turn_with(S8 + " Marienplatz 07:12.", "U5 Ostbahnhof 07:30 Neuperlach")
        checked = turn.check(answer, "en")
        self.assertIn("| S8 | Marienplatz → Flughafen München | 07:12 |", checked)
        self.assertNotIn("| U5", checked)
        self.assertIn("Laimer Platz", checked.rsplit("\n\n", 1)[1])

    def test_the_journey_plan_rows_are_code(self) -> None:
        data = journey.build(
            journey.Journey.model_validate(
                {
                    "destination": "Genève Aéroport",
                    "legs": [
                        {
                            "origin": "Villeurbanne",
                            "destination": "Lyon Part-Dieu",
                            "mode": "local_transit",
                            "service": "Métro A",
                        }
                    ],
                }
            ),
            [],
            "fr",
        )
        # A leg no page gave writes no line.
        self.assertNotIn("Métro A", data["rendered"])
        turn = turn_with()
        turn.compute(data["rendered"])
        self.assertEqual(turn.check(data["rendered"], "fr"), data["rendered"])


class AnchorTests(unittest.TestCase):
    def test_lines_in_several_forms_and_languages(self) -> None:
        sources = route_facts.Sources(
            ["Métro M14 jusqu'à Gare de Lyon", "Le RER B dessert Massy", "U-Bahn U3"],
            [],
        )
        for answer, held in (
            ("prenez la ligne 14", True),
            ("take line B of the RER", True),
            ("nehmen Sie die U3", True),
            ("prenez la ligne 7", False),
            ("take the RER C", False),
        ):
            with self.subTest(answer=answer):
                anchors = route_facts._lines(
                    answer, route_facts.fold(answer), 0, len(answer)
                )
                self.assertEqual(len(anchors), 1, anchors)
                self.assertEqual(sources.together(anchors), held)

    def test_clock_times_and_quantities_are_not_lines(self) -> None:
        for answer in ("le train 06:38", "the bus 5 min later", "train 9h12"):
            with self.subTest(answer=answer):
                self.assertEqual(
                    route_facts._lines(
                        answer, route_facts.fold(answer), 0, len(answer)
                    ),
                    [],
                )

    def test_the_persons_own_address_is_theirs(self) -> None:
        answer = f"Depuis {HOME}, marchez jusqu'à la station Gratte-Ciel."
        turn = turn_with(LINE_A, place=HOME)
        self.assertEqual(turn.check(answer, "fr"), answer)

    def test_sentences_without_route_facts_are_untouched(self) -> None:
        turn = turn_with()
        for answer in (
            "Achetez votre billet directement en ligne. Bon voyage !",
            "Prévoyez une marge à l'arrivée.",
            "Vérifiez les horaires sur le site de l'exploitant.",
        ):
            with self.subTest(answer=answer):
                self.assertEqual(turn.check(answer, "fr"), answer)

    def test_details_review_leaves_route_facts_to_the_travel_check(self) -> None:
        answer = (
            "Descendez à Bellecour, puis prenez la ligne B direction Gare Part-Dieu."
        )
        turn = turn_with()
        own = unsourced_details.Evidence.of([turn.owned_values(answer)])
        nothing = unsourced_details.Evidence.of([])
        left = unsourced_details.unsupported(answer, nothing, own)
        self.assertEqual(left, [], [d.text for d in left])

    def test_a_platform_label_is_no_clock_time(self) -> None:
        answer = (
            "Your train leaves from platform 4 at 08:15 and calls at "
            "Stoke-on-Trent. Platform 9 for the return."
        )
        turn = turn_with(
            "Platform 4: 08:15 to Manchester Piccadilly, calling at Stoke-on-Trent"
        )
        body, note = body_and_note(turn.check(answer, "en"))
        self.assertEqual(
            body,
            "Your train leaves from platform 4 at 08:15 and calls at Stoke-on-Trent.",
        )
        self.assertTrue(note.startswith("Not verified, so left out: “Platform 9”."))


class StructureTests(unittest.TestCase):
    def test_counts_that_announced_removed_items_go(self) -> None:
        for line, count, expected in (
            ("The plan, in three legs:", 3, "The plan:"),
            ("Voici les 3 étapes :", 3, "Voici les étapes :"),
            ("Le trajet en trois étapes :", 3, "Le trajet :"),
            ("Der Plan in drei Abschnitten:", 3, "Der Plan:"),
            ("Two options:", 2, "Options:"),
        ):
            with self.subTest(line=line):
                self.assertEqual(travel_claims._without_count(line, count), expected)

    def test_numbered_lists_and_empty_introductions(self) -> None:
        original = (
            "Itinéraire en deux étapes :\n1. Métro.\n2. Train.\n\n"
            "Autres options :\n- Bus 37.\n\n## Retour\nTrain de 18 h.\n\n"
            "## Billets\nSur le site de l'exploitant."
        )
        edited = (
            "Itinéraire en deux étapes :\n2. Train.\n\nAutres options :\n\n"
            "## Retour\n\n## Billets\nSur le site de l'exploitant."
        )
        self.assertEqual(
            travel_claims._restructure(original, edited),
            "Itinéraire :\n1. Train.\n\n## Billets\nSur le site de l'exploitant.",
        )


if __name__ == "__main__":
    unittest.main()
