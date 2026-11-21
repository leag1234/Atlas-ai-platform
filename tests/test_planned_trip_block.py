"""A planned trip's answer gives the planner's block as code wrote it.

Staging -131 (UI trip battery, 2026-11-19): the trips whose answer was the
code-rendered block (Caen, Munich, New York, Tokyo) were right; the others
were not:

- Madrid to Barcelona Sants: the answer was rewritten as prose; its first
  option lost the AVE leg (the main train) and its second option had no leg.
- Caen to Paris: a rewrite cut short (900 tokens) was replaced by what the
  travel checks kept, and one walk leg went out of the block.
- Villeurbanne to Geneva airport: the plan was doubtful and the search for the
  direct services failed, yet the night chain stayed the answer. The doubtful
  step read the turn's context after the pipeline had reset it.

The answers below are those of -131, with the times of the recorded plans
(tests/fixtures/transit) and fictional streets.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

from services.orchestrator import chat_pipeline, travel_claims
from services.orchestrator import transit_planner as tp
from services.orchestrator.location import Position
from test_transit_planner import NOW, Base, Planner

COMPLETE = "services.orchestrator.unsourced_details._complete"
ATTRIBUTION_URL = "https://transitous.org/sources/"


def planned(
    plan: str,
    arguments: dict[str, Any],
    turn: travel_claims.Turn,
    now: datetime = NOW,
) -> dict[str, Any]:
    """The planner tool run for ``turn`` on a recorded plan."""
    planner = Planner(plan)

    async def fetch(_self: Any, path: str, params: dict[str, str]) -> tuple[int, bytes]:
        return await planner(path, params)

    token = travel_claims.current.set(turn)
    try:
        with (
            patch.object(tp.Client, "_fetch", fetch),
            patch.object(tp, "_now", lambda: now),
        ):
            result = asyncio.run(tp.execute(json.dumps(arguments), 30.0))
    finally:
        travel_claims.current.reset(token)
    data = result["data"]
    assert isinstance(data, dict)
    return data


def checked(
    turn: travel_claims.Turn,
    answer: str,
    language: str,
    question: str,
    rewrite: str | None = None,
    now: datetime = NOW,
) -> tuple[str, AsyncMock]:
    """The pipeline's trip checks, outside the turn's context as in the
    pipeline; the bounded rewrite answers ``rewrite`` (None: it fails).

    The checks read the person's days ("el viernes", "demain") on ``now``,
    the clock the recorded plans are computed on (``planned``): never the
    wall clock (staging -141 posttests: run after midnight in Madrid,
    "el viernes" became the next week's Friday and the opening dating the
    plan's Friday was cut)."""
    call = AsyncMock(
        return_value={"text": rewrite, "cost_eur": "0.0004"}
        if rewrite is not None
        else None
    )
    assert travel_claims.current.get() is None
    real = travel_claims.user_dates

    def on_the_clock(texts: list[str], zone: str = "UTC", at: Any = None) -> list[str]:
        return real(texts, zone, at or now)

    with (
        patch(COMPLETE, call),
        patch.object(travel_claims, "user_dates", on_the_clock),
    ):
        final, _ = asyncio.run(
            chat_pipeline._checked_trip(
                turn, answer, language, question=question, profile="atlas-qwen"
            )
        )
    return final, call


MADRID_QUESTION = (
    "tren a barcelona sants, tengo que estar antes de las 13:00 el viernes, "
    "salgo de calle ficticia 9 madrid"
)

# The -131 Madrid answer (the rewrite of the travel checks), on the recorded
# plan's times.
MADRID_PROSE = (
    "Para llegar a Barcelona-Sants antes de las 13:00 el viernes 20 de noviembre "
    "de 2026, desde Calle Ficticia 9 (Madrid), tienes tres opciones:\n\n"
    "- **Opción 1**: salida a las 07:41, llegada a las 11:12. Camina 6 min "
    "hasta Sol ([transitous.org](https://transitous.org/sources/)), toma el tren "
    "C4a dirección Parla a las 07:47 hasta Madrid-Atocha Cercanías a las "
    "07:52 ([transitous.org](https://transitous.org/sources/)). Camina 13 min "
    "hasta la estación ([transitous.org](https://transitous.org/sources/)).\n"
    "- **Opción 2**: salida a las 08:44, llegada a las 12:13.\n"
    "- **Opción 3**: salida a las 08:44, llegada a las 12:19. Camina 6 min "
    "hasta Sol ([transitous.org](https://transitous.org/sources/)), toma el tren "
    "C3 dirección Aranjuez a las 08:50 hasta Madrid-Atocha Cercanías a "
    "las 08:55 ([transitous.org](https://transitous.org/sources/))."
)

GENEVA_QUESTION = (
    "je dois etre a l'aeroport de geneve demain a 9h, comment j'y vais en "
    "transports en commun ?"
)

# The -131 Geneva answer: the night chain given as the way to make the trip.
GENEVA_NIGHT = (
    "Depuis chez vous, voici un itinéraire en transports en commun pour "
    "être à l’aéroport de Genève demain, vendredi 20 novembre "
    "2026, à 9h. Les horaires ci-dessous sont ceux calculés sur les "
    "données ouvertes ; vérifiez-les auprès des exploitants avant de "
    "partir.\n\n"
    "1. Marchez 14 min jusqu’à Verlaine - Tolstoï, puis prenez le bus "
    "PL4 direction Cordeliers : Verlaine - Tolstoï 03:19 → Saxe - "
    "Lafayette 03:29 ([transitous.org](https://transitous.org/sources/)).\n"
    "2. Marchez 2 min jusqu’à Saxe - Lafayette, puis prenez le bus PL3 "
    "direction Stade de Gerland Le LOU : Saxe - Lafayette 03:43 → Stade de "
    "Gerland Le LOU 03:58 ([transitous.org](https://transitous.org/sources/)).\n"
)


class Planned(Base):
    def madrid(self) -> tuple[travel_claims.Turn, dict[str, Any]]:
        turn = travel_claims.Turn(
            active=True, language="es", zone="Europe/Madrid", asked=[MADRID_QUESTION]
        )
        data = planned(
            "madrid",
            {
                "destination": "Barcelona Sants",
                "origin": "Calle Ficticia 9, Madrid",
                "arrive_by": "2026-11-20T13:00",
            },
            turn,
        )
        self.assertEqual(data["outcome"], "planned")
        return turn, data

    def caen(self) -> tuple[travel_claims.Turn, dict[str, Any]]:
        question = (
            "je dois etre a la gare de lyon a paris avant 12h30 demain, quel "
            "trajet en transports ?"
        )
        turn = travel_claims.Turn(
            active=True,
            language="fr",
            zone="Europe/Paris",
            asked=[question],
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


class PlannedBlockTest(Planned):
    def test_the_plan_and_its_times_are_kept_for_the_answer(self) -> None:
        turn, data = self.madrid()
        self.assertEqual(turn.plan, data["rendered"])
        self.assertEqual(turn.doubtful, "")
        self.assertIn("07:57", turn.plan_times)
        # The person's own deadline is no time of the plan.
        self.assertNotIn("13:00", turn.plan_times)

    def test_a_prose_rewrite_gives_way_to_the_whole_block(self) -> None:
        turn, data = self.madrid()
        final, _ = checked(turn, MADRID_PROSE, "es", MADRID_QUESTION, MADRID_PROSE)
        self.assertIn(data["rendered"], final)
        self.assertEqual(final.count("**Opción 1 (2026-11-20)**"), 1)
        # Every option with all its legs: the AVE of option 1 included.
        self.assertIn(
            "Tren AVE 03081 dirección Barcelona-Sants: Madrid-Puerta de "
            "Atocha-Almudena Grandes 07:57 → Barcelona-Sants 10:59",
            final,
        )
        self.assertNotIn("salida a las 08:44, llegada a las 12:13.", final)
        self.assertNotIn("Camina 13 min hasta la estación", final)
        # The model's opening stays, before the block.
        opening = "Para llegar a Barcelona-Sants antes de las 13:00"
        self.assertTrue(final.startswith(opening), final)

    def test_a_prose_answer_is_repaired_without_its_context(self) -> None:
        turn, data = self.madrid()
        final = tp.planned_answer(MADRID_PROSE, turn)
        self.assertTrue(final.startswith("Para llegar a Barcelona-Sants"), final)
        self.assertIn(data["rendered"], final)
        self.assertNotIn("**Opción 2**: salida a las 08:44", final)

    def test_the_block_given_with_an_opening_is_left_as_written(self) -> None:
        turn, data = self.madrid()
        answer = (
            "Para el viernes 20 de noviembre tienes estas opciones:\n\n"
            + data["rendered"]
        )
        final, call = checked(turn, answer, "es", MADRID_QUESTION)
        self.assertEqual(final, answer)
        call.assert_not_awaited()

    def test_a_block_cut_by_the_checks_is_given_whole(self) -> None:
        turn, data = self.caen()
        opening = (
            "Voici les départs possibles demain, **vendredi 20 novembre "
            "2026**, pour arriver à **Gare de Lyon avant 12:30** :"
        )
        cut = data["rendered"].replace("- Marche 2 min de Gare SNCF à Caen\n", "")
        self.assertNotEqual(cut, data["rendered"])
        final = tp.planned_answer(opening + "\n\n" + cut, turn)
        self.assertEqual(final, opening + "\n\n" + data["rendered"])

    def test_the_checks_never_read_the_block(self) -> None:
        turn, data = self.caen()
        answer = "Voici les départs possibles demain :\n\n" + data["rendered"]
        rest, first = tp.without_plans(answer, turn)
        self.assertEqual(rest, "Voici les départs possibles demain :")
        self.assertFalse(first)
        rest, first = tp.without_plans(
            data["rendered"] + "\n\n<details>x</details>", turn
        )
        self.assertEqual(rest, "<details>x</details>")
        self.assertTrue(first)

    def test_an_answer_that_drops_the_plan_gets_it_after_its_opening(self) -> None:
        turn, data = self.madrid()
        answer = (
            "El AVE es la opción más rápida.\n\n"
            "Compra el billete con antelación en la web del operador."
        )
        final = tp.planned_answer(answer, turn)
        self.assertEqual(
            final,
            "El AVE es la opción más rápida.\n\n"
            + data["rendered"]
            + "\n\nCompra el billete con antelación en la web del operador.",
        )

    def test_a_block_that_opened_the_answer_opens_it_again(self) -> None:
        turn, data = self.madrid()
        advice = "Compra el billete con antelación."
        final = tp.planned_answer(advice, turn, first=True)
        self.assertEqual(final, data["rendered"] + "\n\n" + advice)

    def test_lines_citing_a_page_read_are_never_taken_for_the_plan(self) -> None:
        turn, data = self.madrid()
        fare = (
            "El AVE de las 07:57 cuesta desde 45 euros "
            "([example-rail.test](https://www.example-rail.test/madrid-barcelona))."
        )
        final = tp.planned_answer(fare, turn)
        self.assertIn(fare, final)
        self.assertIn(data["rendered"], final)

    def test_good_trip_answers_are_unchanged(self) -> None:
        # Staging -131: Caen, Munich, New York and Tokyo answered with the block.
        cases: list[
            tuple[str, dict[str, Any], str, tuple[float, float] | None, str]
        ] = [
            (
                "munich",
                {
                    "destination": "Berlin Hauptbahnhof",
                    "origin": "Lindenweg 4, Munich",
                    "arrive_by": "2026-11-20T14:00",
                },
                "en",
                None,
                "Europe/Berlin",
            ),
            (
                "tokyo",
                {
                    "destination": "Kyoto Station",
                    "from_device_position": True,
                    "arrive_by": "2026-11-20T10:00",
                },
                "en",
                (35.681, 139.767),
                "Asia/Tokyo",
            ),
            (
                "caen",
                {
                    "destination": "Paris Gare de Lyon",
                    "from_device_position": True,
                    "arrive_by": "2026-11-20T12:30",
                },
                "fr",
                (49.181, -0.364),
                "Europe/Paris",
            ),
        ]
        for plan, arguments, language, position, zone in cases:
            with self.subTest(plan=plan):
                question = "how do I get there by public transport, friday?"
                turn = travel_claims.Turn(
                    active=True,
                    language=language,
                    zone=zone,
                    asked=[question],
                    position=Position(latitude=position[0], longitude=position[1])
                    if position
                    else None,
                )
                data = planned(plan, arguments, turn)
                self.assertEqual(data["outcome"], "planned")
                final, call = checked(turn, data["rendered"], language, question)
                self.assertEqual(final, data["rendered"])
                call.assert_not_awaited()


class DoubtfulBlockTest(Base):
    def geneva(self) -> tuple[travel_claims.Turn, dict[str, Any]]:
        turn = travel_claims.Turn(
            active=True,
            language="fr",
            asked=[GENEVA_QUESTION],
            zone="Europe/Paris",
            position=Position(latitude=45.769, longitude=4.882),
        )
        data = planned(
            "lyon",
            {
                "destination": "Geneva Airport",
                "from_device_position": True,
                "arrive_by": "2026-11-20T09:00",
            },
            turn,
            now=datetime(2026, 11, 19, 18, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(data["outcome"], "doubtful")
        return turn, data

    def test_a_failed_search_still_says_no_reasonable_connection_first(self) -> None:
        turn, data = self.geneva()
        self.assertEqual(turn.plan, "")
        # The direct-services search failed (engines blocked, quotas spent).
        token = travel_claims.current.set(turn)
        try:
            tp.with_direct_pages(data, [])
        finally:
            travel_claims.current.reset(token)
        final, _ = checked(turn, GENEVA_NIGHT, "fr", GENEVA_QUESTION, GENEVA_NIGHT)
        self.assertTrue(
            final.startswith("Aucune liaison raisonnable en transports en commun"),
            final,
        )
        self.assertEqual(final.count("Solution de repli seulement"), 1)
        self.assertIn(data["rendered"], final)
        self.assertNotIn("Verlaine - Tolstoï 03:19 → Saxe - Lafayette 03:29 (", final)
        self.assertLess(
            final.index("Aucune liaison"), final.index("Solution de repli seulement")
        )

    def test_the_fallback_block_alone_gets_the_statement(self) -> None:
        turn, data = self.geneva()
        final, call = checked(turn, data["rendered"], "fr", GENEVA_QUESTION)
        self.assertTrue(final.startswith("Aucune liaison raisonnable"), final)
        self.assertTrue(final.rstrip().endswith(data["rendered"].rstrip()), final)
        call.assert_not_awaited()

    def test_a_later_plausible_plan_replaces_a_doubtful_one(self) -> None:
        turn, _ = self.geneva()
        data = planned(
            "munich",
            {
                "destination": "Berlin Hauptbahnhof",
                "origin": "Lindenweg 4, Munich",
                "arrive_by": "2026-11-20T14:00",
            },
            turn,
        )
        self.assertEqual(turn.doubtful, "")
        self.assertEqual(turn.plan, data["rendered"])
