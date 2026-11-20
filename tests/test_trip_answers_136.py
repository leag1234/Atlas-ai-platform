"""Trip answers of staging -136 (UI trip battery, 2026-11-20 06:04-06:13 UTC).

- New York: the answer opened with the model's line "Public transport plan:
  your position → South Station (Boston) — for tomorrow, Saturday
  2026-11-21, arriving by 11:00:" right above the block's own title. The
  line is a copy of the title: the day and the deadline it adds are in the
  block already.
- Villeurbanne to Geneva airport (planned_late: nothing reasonable arrives
  by 09:00): advice to aim for 08:30 at the airport, which no option of the
  plan reaches either, was quoted in a removal note as an unverified fact.
  A removal note is for facts the person might rely on; unsourced advice
  goes without one.

The other answers of the battery (Caen, Munich, Tokyo, Sydney, Madrid, the
late Geneva plan with its lateness lines) were right and stay as they are.
The clock is patched where a plan is computed; questions give their day;
streets are fictional.
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from services.orchestrator import travel_claims
from services.orchestrator import transit_planner as tp
from services.orchestrator.location import Position
from test_planned_trip_block import MADRID_QUESTION, checked, planned
from test_trip_answers_132 import CAEN_QUESTION
from test_trip_answers_134 import TOKYO_QUESTION, Trips134
from test_person_local_day import MUNICH_QUESTION, NYC_DATED, SYDNEY_QUESTION
from test_trip_answers_135 import HOME, NOW, GenevaPlanner

GENEVA_QUESTION = (
    "je dois etre a l'aeroport de geneve le 2026-11-21 a 9h, comment j'y vais "
    "depuis chez moi en transport en commun ?"
)


def title(block: str) -> str:
    """The block's title line without its emphasis."""
    return block.splitlines()[0].strip("*")


class Trips136(Trips134):
    def caen_dated(self) -> tuple[travel_claims.Turn, dict[str, Any]]:
        """Caen with the trip's day in the question (never today's date)."""
        turn = travel_claims.Turn(
            active=True,
            language="fr",
            zone="Europe/Paris",
            asked=[CAEN_QUESTION],
            position=Position(latitude=49.181, longitude=-0.364),
        )
        data = planned(
            "caen",
            {
                "destination": "Paris Gare de Lyon",
                "from_device_position": True,
                "arrive_by": "2026-11-20T12:30",
            },
            turn,
        )
        self.assertEqual(data["outcome"], "planned")
        return turn, data

    def geneva(self) -> tuple[travel_claims.Turn, dict[str, Any]]:
        """The late plan of -135/-136: the first regional train arrives at
        09:08 for 09:00."""
        planner = GenevaPlanner()
        turn = travel_claims.Turn(
            active=True,
            language="fr",
            asked=[GENEVA_QUESTION],
            zone="Europe/Paris",
            position=Position(latitude=HOME[0], longitude=HOME[1]),
        )

        async def fetch(
            _self: Any, path: str, params: dict[str, str]
        ) -> tuple[int, bytes]:
            return await planner(path, params)

        token = travel_claims.current.set(turn)
        try:
            with (
                patch.object(tp.Client, "_fetch", fetch),
                patch.object(tp, "_now", lambda: NOW),
            ):
                result = asyncio.run(
                    tp.execute(
                        json.dumps(
                            {
                                "destination": "aéroport de Genève, Le Grand-Saconnex",
                                "from_device_position": True,
                                "arrive_by": "2026-11-21T09:00",
                            }
                        ),
                        30.0,
                    )
                )
        finally:
            travel_claims.current.reset(token)
        data = result["data"]
        assert isinstance(data, dict)
        self.assertEqual(data["outcome"], "planned_late")
        return turn, data


def block_in(language: str, origin: str, destination: str) -> str:
    """A small block as the planner writes it, in ``language``."""
    w = tp.WORDS[language]
    return "\n\n".join(
        [
            f"**{w['title']}: {origin} → {destination}**",
            "**"
            + w["option"].format(n=1)
            + " (2026-11-21)**: "
            + w["summary"].format(
                d="07:35", a="10:38", m="3 h 03 min", t=w["transfers"][0]
            ),
            "- " + w["walk_end"].format(m="4 min", b=destination),
            "- " + w["deadline"].format(a="10:38", d="11:00"),
            w["zones"].format(z="Europe/Paris"),
        ]
    )


def plan_turn(block: str, language: str) -> SimpleNamespace:
    return SimpleNamespace(
        plan=block,
        doubtful="",
        plan_times={"07:35", "10:38"},
        language=language,
        plan_starts=[],
    )


class TitleCopyTest(Trips136):
    def test_the_new_york_title_copy_goes(self) -> None:
        turn, data = self.new_york()
        block = data["rendered"]
        copy = f"{title(block)} — for tomorrow, Friday 2026-11-20, arriving by 11:00:"
        final, _ = checked(turn, copy + "\n\n" + block, "en", NYC_DATED)
        self.assertEqual(final, block, final)

    def test_title_copies_go_in_each_language(self) -> None:
        for language, origin, destination, copies in (
            (
                "en",
                "your position",
                "Central Station",
                [
                    "Public transport plan: your position → Central Station "
                    "— for tomorrow, Saturday 2026-11-21, arriving by 11:00:",
                    "**Public transport plan: your position → Central Station** "
                    "(tomorrow, before 11:00)",
                    "Public transport plan: from your position → Central "
                    "Station, Saturday 21 November:",
                    "### Public transport plan for tomorrow",
                ],
            ),
            (
                "fr",
                "votre position",
                "Gare Centrale",
                [
                    "Itinéraire en transports en commun : votre position → "
                    "Gare Centrale, pour demain samedi 21 novembre, avant 11h00 :",
                    "Itinéraire en transports en commun pour demain :",
                ],
            ),
            (
                "de",
                "Ihr Standort",
                "Hauptbahnhof",
                [
                    "Verbindung mit öffentlichen Verkehrsmitteln: Ihr Standort "
                    "→ Hauptbahnhof – morgen, Samstag, 21. November, bis "
                    "11:00:",
                ],
            ),
            (
                "es",
                "su posición",
                "Estación Central",
                [
                    "Itinerario en transporte público: su posición → Estación "
                    "Central, mañana sábado 21 de noviembre, antes de las 11:00:",
                ],
            ),
            (
                "it",
                "la tua posizione",
                "Stazione Centrale",
                [
                    "Itinerario con i mezzi pubblici: la tua posizione → "
                    "Stazione Centrale, domani sabato 21 novembre, entro le 11:00:",
                ],
            ),
        ):
            block = block_in(language, origin, destination)
            for copy in copies:
                with self.subTest(language=language, copy=copy):
                    final = tp.planned_answer(
                        copy + "\n\n" + "Bon voyage !",
                        plan_turn(block, language),
                    )
                    self.assertEqual(final, block + "\n\nBon voyage !", final)

    def test_what_a_title_copy_adds_stays_as_a_sentence(self) -> None:
        block = block_in("en", "your position", "Central Station")
        final = tp.planned_answer(
            "Public transport plan: your position → Central Station — "
            "the coach is the cheapest option:",
            plan_turn(block, "en"),
        )
        # Kept, it is the answer's first paragraph: the block follows it.
        self.assertEqual(final, "The coach is the cheapest option.\n\n" + block)
        final = tp.planned_answer(
            "Public transport plan: from your flat → the station: bring "
            "your rail card.",
            plan_turn(block, "en"),
        )
        self.assertEqual(final, "Bring your rail card.\n\n" + block)

    def test_sentences_about_the_plan_are_no_title_copy(self) -> None:
        block = block_in("en", "your position", "Central Station")
        for sentence in (
            "Public transport planning is easy on this route.",
            "Public transport plan is the cheapest way there.",
            "The public transport plan below leaves early.",
            "Here is your public transport plan to Central Station:",
        ):
            with self.subTest(sentence=sentence):
                final = tp.planned_answer(sentence, plan_turn(block, "en"))
                self.assertIn(sentence.rstrip(":"), final)
        cited = (
            "Public transport plan: your position → Central Station, as "
            "[the operator](https://operator.example/plan) shows it."
        )
        final = tp.planned_answer(cited, plan_turn(block, "en"))
        self.assertIn(cited, final)

    def test_a_title_copy_with_another_time_says_something(self) -> None:
        block = block_in("en", "your position", "Central Station")
        self.assertTrue(tp._adds_to_title("arriving by 09:30", block, "en"))
        self.assertTrue(tp._adds_to_title("on 2026-11-22", block, "en"))
        self.assertFalse(tp._adds_to_title("arriving by 11:00", block, "en"))
        self.assertFalse(tp._adds_to_title("Saturday 2026-11-21", block, "en"))


GENEVA_ADVICE = (
    "Si votre rendez-vous est strict à 9h00, prévoyez de viser plutôt 8h30 à "
    "l'aéroport."
)


class AdviceTest(Trips136):
    def test_advice_the_late_plan_contradicts_goes_without_a_note(self) -> None:
        turn, data = self.geneva()
        block = data["rendered"]
        for advice in (
            GENEVA_ADVICE,
            "Aucun itinéraire n'arrive avant 9h00 : prévoyez de viser plutôt "
            "8h30 à l'aéroport si votre rendez-vous est strict.",
        ):
            with self.subTest(advice=advice):
                final, call = checked(
                    turn, block + "\n\n" + advice, "fr", GENEVA_QUESTION
                )
                self.assertEqual(final, block, final)
                self.assertNotIn("Non vérifié", final)
                # Nothing to rewrite: advice only went.
                call.assert_not_awaited()

    def test_a_fact_keeps_its_note(self) -> None:
        turn, data = self.geneva()
        block = data["rendered"]
        for fact, quoted in (
            ("Un TER part aussi de Lyon Part-Dieu à 5h52.", "5h52"),
            ("Je vous conseille de prendre le TER de 5h52.", "5h52"),
            ("Prévoyez de viser 8h30 ; un car part aussi à 5h52.", "5h52"),
        ):
            with self.subTest(fact=fact):
                final, _ = checked(turn, block + "\n\n" + fact, "fr", GENEVA_QUESTION)
                self.assertNotIn(fact, final)
                note = final.split("Non vérifié", 1)
                self.assertEqual(len(note), 2, final)
                self.assertIn(quoted, note[1])
                self.assertNotIn("8h30", note[1])
                self.assertTrue(final.startswith(block), final)

    def test_advice_and_facts_in_each_language(self) -> None:
        advice = (
            "Aim to be at the terminal by 06:30.",
            "I'd recommend arriving at 06:30 to be safe.",
            "Prévoyez d'être à l'aéroport vers 6h30.",
            "Je vous conseille, si possible, d'arriver à 6h30.",
            "Planen Sie, um 06:30 am Flughafen zu sein.",
            "Am besten sind Sie um 06:30 dort.",
            "Procure estar en el aeropuerto a las 06:30.",
            "Le recomiendo llegar a las 06:30.",
            "Cerchi di essere in aeroporto alle 06:30.",
            "Le consiglio di arrivare alle 06:30.",
        )
        facts = (
            "The last bus leaves at 06:30.",
            "Take the 06:30 train.",
            "I'd recommend the T8 at 06:30.",
            "Prévoyez du temps : le car part à 6h30.",
            "Je vous conseille le TER de 6h30.",
            "Nehmen Sie den Zug um 06:30.",
            "Le recomiendo el tren de las 06:30.",
            "Le consiglio il treno delle 06:30.",
            "Le premier départ est à 6h30.",
        )
        for text, expected in [(t, True) for t in advice] + [(t, False) for t in facts]:
            with self.subTest(text=text):
                clocks = [
                    q for q in travel_claims.quantities(text) if q.kind == "clock"
                ]
                self.assertEqual(len(clocks), 1, clocks)
                self.assertEqual(travel_claims._advice(text, clocks[0]), expected)


class GoodAnswersTest(Trips136):
    """The answers of -136 that were right stay as they were."""

    def test_the_good_answers_are_unchanged(self) -> None:
        cases = {
            "caen": (
                self.caen_dated,
                CAEN_QUESTION,
                "Voici les départs possibles le 2026-11-20 depuis chez toi "
                "pour arriver à Gare de Lyon avant 12:30 :",
                "",
            ),
            "munich": (
                self.munich,
                MUNICH_QUESTION,
                "From your position on Lindenweg, you can reach Berlin Hbf by "
                "train on Friday 2026-11-20, arriving well before 2pm. Here are "
                "three options:",
                "",
            ),
            "tokyo": (self.tokyo, TOKYO_QUESTION, "", ""),
            "sydney": (
                self.sydney,
                SYDNEY_QUESTION,
                "For your 8:00 AM flight on Friday 2026-11-20, the fastest public "
                "transport option from your street is the T8 train straight to "
                "the airport. Aim to be at the terminal by about 7:00 AM.",
                "",
            ),
            "madrid": (
                self.madrid,
                MADRID_QUESTION,
                "El viernes 20 de noviembre de 2026, desde Calle Ficticia 9 "
                "(Madrid), tienes tres opciones para llegar a Barcelona-Sants "
                "antes de las 13:00:",
                "Todas llegan antes de las 13:00; la opción 3 te deja el menor "
                "margen, mientras que la opción 1 te da casi dos horas.",
            ),
        }
        for name, (trip, question, opening, closing) in cases.items():
            with self.subTest(trip=name):
                turn, data = trip()
                block = data["rendered"]
                answer = "\n\n".join(p for p in (opening, block, closing) if p)
                final, _ = checked(turn, answer, turn.language, question)
                self.assertEqual(final, answer, final)

    def test_the_late_geneva_plan_keeps_its_lateness_lines(self) -> None:
        turn, data = self.geneva()
        block = data["rendered"]
        opening = (
            "Voici ce que le calculateur d'itinéraires trouve pour le 2026-11-21 "
            "depuis votre position jusqu'à l'aéroport de Genève pour 9h00 :"
        )
        answer = opening + "\n\n" + block
        final, _ = checked(turn, answer, "fr", GENEVA_QUESTION)
        self.assertEqual(final, answer, final)
        for line in (
            "Aucun itinéraire raisonnable trouvé n’arrive au plus tard à 09:00 : ",
            "Arrivée 09:08, 8 min après l’heure demandée (09:00).",
            "TER 96500",
        ):
            self.assertIn(line, final)
