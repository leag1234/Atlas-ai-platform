"""Trip answers of staging -141 (UI trip battery, 2026-11-20 23:25-23:32 UTC).

- Villeurbanne to Geneva airport "tomorrow at 9", asked late on Friday night:
  tomorrow is Sunday 2026-11-22. Only night coaches arrive in time, and the
  second search (arrivals up to 30 minutes late) found nothing reasonable on a
  Sunday morning, so the night chain stayed the answer. Measured on
  api.transitous.org (tests/fixtures/transit/geneva-sunday-*, trimmed to the
  fields read): the first reasonable arrival of that Sunday is TER 96504,
  Lyon Part-Dieu 08:38 to Geneva Cornavin 10:40, at the airport 11:07. The
  search now widens (+30 min, +2 h, then the day's departures) and gives
  that arrival first; a night itinerary that arrives in time follows, said as
  night travel, never as the answer.
- The same answer opened with "The pages read about this trip give its
  timetable: transitous.org, gitesjura-lacheminee.com, tpg.ch.": no time
  quoted, the journey planner's own page and a holiday cottage's contact page
  listed. A sentence about the pages now quotes what they say of the trip's
  times, or is not written.
- Sydney: "The walk from your position to the stations is not covered by the
  sources" under a plan whose every itinerary starts with that walk.
- Posttests of -141: the Madrid answer's checks read "el viernes" on the wall
  clock, after midnight in Madrid; they now read it on the plan's clock.

Streets are fictional; the clock is patched wherever a plan is computed.
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

from services.orchestrator import travel_claims, travel_rebuild
from services.orchestrator import transit_planner as tp
from services.orchestrator.location import Position
from test_person_local_day import SYDNEY_QUESTION, TripTextTest
from test_planned_trip_block import MADRID_QUESTION, checked
from test_transit_planner import Base, recorded
from test_trip_answers_135 import HOME, GenevaPlanner

SUNDAY_QUESTION = (
    "je dois etre a l'aeroport de geneve demain a 9h, comment j'y vais depuis "
    "chez moi en transport en commun ?"
)
# Friday 23:25 UTC: 00:25 on Saturday in Villeurbanne, so tomorrow is Sunday.
LATE_FRIDAY = datetime(2026, 11, 20, 23, 25, tzinfo=timezone.utc)
SUNDAY_PLANS = {
    ("2026-11-22T09:00+01:00", "true"): "geneva-sunday-0900",
    ("2026-11-22T09:30+01:00", "true"): "geneva-sunday-0930",
    ("2026-11-22T11:00+01:00", "true"): "geneva-sunday-1100",
    ("2026-11-22T05:00+01:00", "false"): "geneva-sunday-day",
}
TIMETABLE = "https://bus.example.test/lignes/5"
COTTAGE = "https://cottage.example.test/contact"


class SundayPlanner(GenevaPlanner):
    """The recorded Sunday answers, by time and direction of the search."""

    async def __call__(self, path: str, params: dict[str, str]) -> tuple[int, bytes]:
        if not path.endswith("/plan"):
            return await super().__call__(path, params)
        self.calls.append((path, params))
        name = SUNDAY_PLANS.get((params["time"], params["arriveBy"]), "")
        body = recorded(name) if name else {"itineraries": [], "direct": []}
        return 200, json.dumps(body).encode()


def sunday(
    until: float | None = None,
) -> tuple[SundayPlanner, travel_claims.Turn, dict[str, Any]]:
    planner = SundayPlanner()
    turn = travel_claims.Turn(
        active=True,
        language="fr",
        zone="Europe/Paris",
        asked=[SUNDAY_QUESTION],
        position=Position(latitude=HOME[0], longitude=HOME[1]),
    )

    async def fetch(_self: Any, path: str, params: dict[str, str]) -> tuple[int, bytes]:
        return await planner(path, params)

    arguments = {
        "destination": "aéroport de Genève, Le Grand-Saconnex",
        "from_device_position": True,
        "arrive_by": "2026-11-22T09:00",
    }
    token = travel_claims.current.set(turn)
    try:
        with (
            patch.object(tp.Client, "_fetch", fetch),
            patch.object(tp, "_now", lambda: LATE_FRIDAY),
        ):
            if until is None:
                result = asyncio.run(tp.execute(json.dumps(arguments), 30.0))
            else:
                config = tp.settings()
                assert config is not None
                result = {
                    "data": asyncio.run(
                        tp.compute(
                            tp.TransitPlan(**arguments),
                            config=config,
                            language="fr",
                            position=HOME,
                            home_zone="Europe/Paris",
                            days=["2026-11-22"],
                            now=LATE_FRIDAY,
                            until=until,
                        )
                    )
                }
    finally:
        travel_claims.current.reset(token)
    data = result["data"]
    assert isinstance(data, dict)
    return planner, turn, data


class WidenedSearchTest(Base):
    def test_the_first_reasonable_arrival_of_the_day_comes_first(self) -> None:
        planner, turn, data = sunday()
        self.assertEqual(data["outcome"], "planned_late")
        self.assertEqual(data["day"], "2026-11-22")
        calls = [
            (p["time"], p["arriveBy"]) for _, p in planner.calls if _.endswith("/plan")
        ]
        # The first search, then +30 min, +2 h and the day's departures.
        self.assertEqual(calls, list(SUNDAY_PLANS))
        block = data["rendered"]
        self.assertTrue(block.startswith("**Itinéraire en transports en commun"), block)
        self.assertIn(
            "La première arrivée raisonnable trouvée est à 11:07, 2 h 07 min "
            "après l’heure demandée (09:00).",
            block,
        )
        self.assertIn(
            "Train TER 96504 : Lyon Part Dieu 08:38 → Genève Cornavin 10:40", block
        )
        # The late options come before the night one, which is said as such.
        night = "Seulement si vous pouvez voyager de nuit : cet itinéraire arrive à l’heure, avec un départ à 03:04."
        self.assertIn(night, block)
        self.assertLess(block.index("TER 96504"), block.index(night))
        self.assertLess(block.index(night), block.index("BlaBlaCar"))
        self.assertIn("Arrivée 06:47 pour 09:00 demandé.", block)
        self.assertEqual(data["night_option"], "2026-11-22T03:04+01:00")
        # Seven hours early at an airport is no option: the 22:34 coach goes.
        self.assertNotIn("22:34 →", block)
        self.assertNotIn("FlixBus N122", block)
        # The plan is the answer's block, not a doubtful fallback.
        self.assertEqual(turn.plan, block)
        self.assertEqual(turn.doubtful, "")
        self.assertIn("11:07", turn.plan_times)
        self.assertEqual(turn.clock, LATE_FRIDAY)

    def test_no_widening_once_the_tool_time_is_spent(self) -> None:
        planner, _, data = sunday(until=time.monotonic())
        self.assertEqual(data["outcome"], "doubtful")
        self.assertEqual(planner.plans(), 2)

    def test_a_late_arrival_within_half_an_hour_gets_no_night_option(self) -> None:
        late = [
            {
                "depart": datetime(2026, 11, 21, 6, 0, tzinfo=timezone.utc),
                "arrive": datetime(2026, 11, 21, 8, 10, tzinfo=timezone.utc),
                "latest": datetime(2026, 11, 21, 8, 10, tzinfo=timezone.utc),
                "minutes": 130,
            }
        ]
        night = [
            {
                "depart": datetime(2026, 11, 21, 3, 0, tzinfo=timezone.utc),
                "arrive": datetime(2026, 11, 21, 7, 0, tzinfo=timezone.utc),
                "latest": datetime(2026, 11, 21, 7, 0, tzinfo=timezone.utc),
                "minutes": 240,
            }
        ]
        deadline = datetime(2026, 11, 21, 8, 0, tzinfo=timezone.utc)
        now = datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)
        self.assertIsNone(tp.night_option(night, late, deadline, now))
        later = [
            {
                **late[0],
                "arrive": deadline.replace(hour=9),
                "latest": deadline.replace(hour=9),
            }
        ]
        self.assertIs(tp.night_option(night, later, deadline, now), night[0])


class PagesSentenceTest(Base):
    TIMES = (
        "Ligne 5 : départs de Genève, gare Cornavin vers Genève-Aéroport à 06:12, "
        "06:24 et 06:36 le dimanche."
    )

    def turn(self) -> travel_claims.Turn:
        turn = travel_claims.Turn(
            active=True,
            language="fr",
            zone="Europe/Paris",
            asked=[SUNDAY_QUESTION],
            place="Rue Fictive 3, Villeurbanne, France",
        )
        turn.collect(
            {
                "url": tp.SOURCES_URL,
                "text": "Genève-Aéroport. Train IR95 : Genève 06:40 → Genève-Aéroport 06:47.",
            }
        )
        turn.collect(
            {
                "url": COTTAGE,
                "text": (
                    "Accès au gîte. En avion : Genève, aéroport international à 60 km, "
                    "location de voiture à l'aéroport. Lyon Saint-Exupéry, aéroport "
                    "international à 125 km. En train : gare de Bellegarde."
                ),
            }
        )
        turn.collect({"url": TIMETABLE, "text": self.TIMES})
        return turn

    def test_the_sentence_quotes_what_the_pages_say(self) -> None:
        chosen = travel_rebuild.excerpts(self.turn(), SUNDAY_QUESTION)
        pointer = travel_rebuild._pointer(chosen, "fr")
        self.assertTrue(
            pointer.startswith(
                "Les pages consultées sur ce trajet indiquent : « Ligne 5"
            ),
            pointer,
        )
        self.assertIn(
            "06:12, 06:24 et 06:36 le dimanche » ([bus.example.test]("
            + TIMETABLE
            + "))",
            pointer,
        )
        # Never the journey planner's own page, nor a page that gives no time
        # of the trip.
        self.assertNotIn("transitous", pointer)
        self.assertNotIn("cottage", pointer)

    def test_no_time_quoted_no_sentence(self) -> None:
        turn = self.turn()
        turn.linked = [(url, text) for url, text in turn.linked if url != TIMETABLE]
        chosen = travel_rebuild.excerpts(turn, SUNDAY_QUESTION)
        self.assertEqual(travel_rebuild._pointer(chosen, "fr"), "")
        for language in ("en", "de", "es", "it"):
            self.assertEqual(travel_rebuild._pointer(chosen, language), "")

    def test_each_language_quotes(self) -> None:
        excerpt = travel_rebuild.Excerpt(TIMETABLE, self.TIMES, named=True)
        for language, opening in (
            ("en", "The pages read about this trip say: “Ligne 5"),
            ("de", "Die gelesenen Seiten zu dieser Reise geben an: „Ligne 5"),
            ("es", "Las páginas consultadas sobre este viaje indican: “Ligne 5"),
            ("it", "Le pagine consultate su questo viaggio indicano: “Ligne 5"),
        ):
            pointer = travel_rebuild._pointer([excerpt], language)
            self.assertTrue(pointer.startswith(opening), pointer)


class StartWalkCaveatTest(TripTextTest):
    def test_the_walk_to_the_stations_caveat_goes(self) -> None:
        turn, data = self.sydney()
        self.assertTrue(turn.plan_walks_first)
        caveat = (
            "The walk from your position to the stations is not covered by the "
            "sources, so verify the exact route with a map or the operator’s "
            "journey planner."
        )
        final, _ = checked(
            turn, data["rendered"] + "\n\n" + caveat, "en", SYDNEY_QUESTION
        )
        self.assertEqual(final, data["rendered"], final)

    def test_any_stop_wording_in_each_language(self) -> None:
        turn, _ = self.sydney()
        covered = tp._start_covered(turn)
        assert covered is not None
        for sentence in (
            "The route from your position to the first stop is not covered by the sources.",
            "The walking part from where you are is not given by the sources.",
            "Le trajet à pied depuis votre position jusqu'aux gares n'est pas couvert par les sources.",
            "Der Fußweg von Ihrem Standort zur Haltestelle ist in den Quellen nicht angegeben.",
            "El camino desde tu posición hasta la estación no figura en las fuentes.",
            "Il percorso dalla sua posizione alla fermata non è indicato dalle fonti.",
        ):
            self.assertTrue(covered(sentence), sentence)
        for sentence in (
            # Not about the way there.
            "From your position, the platforms at the station are not given by the sources.",
            # No start: another walk.
            "The walk between the two stations is not covered by the sources.",
            # A time says more than the plan.
            "From your position, the 06:19 train at the station is not confirmed by the sources.",
        ):
            self.assertFalse(covered(sentence), sentence)

    def test_a_plan_starting_with_a_ride_keeps_it(self) -> None:
        turn, _ = self.sydney()
        turn.plan_walks_first = False
        covered = tp._start_covered(turn)
        assert covered is not None
        self.assertFalse(
            covered(
                "The walk from your position to the stations is not covered by the sources."
            )
        )


class PlanClockTest(TripTextTest):
    def test_the_checks_read_the_days_on_the_plan_clock(self) -> None:
        turn, data = self.madrid()
        self.assertIsNotNone(turn.clock)
        opening = (
            "El viernes 20 de noviembre de 2026, desde Calle Ficticia 9 (Madrid), "
            "tienes tres opciones para llegar a Barcelona-Sants antes de las 13:00:"
        )
        answer = opening + "\n\n" + data["rendered"]
        # The checks run after midnight in Madrid, on Saturday: "el viernes" is
        # still the plan's Friday.
        saturday = datetime(2026, 11, 20, 23, 30, tzinfo=timezone.utc)
        with patch("services.orchestrator.datecalc.utc_now", return_value=saturday):
            final, _ = checked(turn, answer, "es", MADRID_QUESTION)
            self.assertEqual(turn.trip_dates(), ["2026-11-20"])
            self.assertEqual(turn.today(), "2026-11-19")
        self.assertEqual(final, answer, final)
        # Without a plan, the orchestrator's clock.
        bare = travel_claims.Turn(
            active=True, zone="Europe/Madrid", asked=[MADRID_QUESTION]
        )
        with patch("services.orchestrator.datecalc.utc_now", return_value=saturday):
            self.assertEqual(bare.trip_dates(), ["2026-11-27"])
            self.assertEqual(bare.today(), "2026-11-21")
