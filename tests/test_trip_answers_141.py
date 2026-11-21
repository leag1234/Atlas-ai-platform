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
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

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
                            tp.TransitPlan.model_validate(arguments),
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
    def test_the_arrival_nearest_the_time_asked_comes_first(self) -> None:
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
            "Aucun itinéraire trouvé n’arrive au plus tard à 09:00 sans voyager de "
            "nuit. Les options ci-dessous arrivent après 09:00 ; la première arrive "
            "à 09:15, 15 min après l’heure demandée.",
            block,
        )
        # The coach arriving 15 min late is slow (3 h 37 for 109 km): no
        # faster itinerary arrives near it, so it stays, said to be long.
        coach = block.index("Car FlixBus N788")
        self.assertIn(
            "- Trajet long : 3 h 37 min ; aucun itinéraire plus rapide trouvé "
            "n’arrive à une heure comparable.\n- Arrivée 09:15, 15 min après "
            "l’heure demandée (09:00).",
            block,
        )
        # The faster train arriving later follows.
        train = block.index(
            "Train TER 96504 : Lyon Part Dieu 08:38 → Genève Cornavin 10:40"
        )
        self.assertLess(coach, train)
        self.assertIn(
            "Arrivée 11:07, 2 h 07 min après l’heure demandée (09:00).", block
        )
        self.assertEqual(block.count("Trajet long"), 1)
        # 15 min late: no night travel offered; no night chain at all.
        self.assertNotIn("night_option", data)
        self.assertNotIn("Seulement si vous pouvez voyager de nuit", block)
        self.assertNotIn("BlaBlaCar", block)
        self.assertNotIn("FlixBus N122", block)
        self.assertEqual(data["itineraries"][0]["long"], True)
        # The plan is the answer's block, not a doubtful fallback.
        self.assertEqual(turn.plan, block)
        self.assertEqual(turn.doubtful, "")
        self.assertIn("09:15", turn.plan_times)
        self.assertEqual(turn.clock, LATE_FRIDAY)

    def test_no_widening_once_the_tool_time_is_spent(self) -> None:
        # The +30 min search still runs: its long coach is the answer alone.
        planner, _, data = sunday(until=time.monotonic())
        self.assertEqual(data["outcome"], "planned_late")
        self.assertEqual(planner.plans(), 2)
        self.assertIn("Car FlixBus N788", data["rendered"])
        self.assertNotIn("TER 96504", data["rendered"])

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


def leg(mode: str, name: str, start: str, end: str, zone: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "startTime": start,
        "endTime": end,
        "scheduledStartTime": start,
        "scheduledEndTime": end,
        "displayName": name,
        "from": {"name": "Ferry Quay", "tz": zone},
        "to": {"name": "Harbour Terminal", "tz": zone},
    }


def row(mode: str, name: str, start: str, end: str, zone: str) -> dict[str, Any]:
    return {
        "startTime": start,
        "endTime": end,
        "transfers": 0,
        "legs": [leg(mode, name, start, end, zone)],
    }


class SlowOptionTest(unittest.TestCase):
    """Any mode, any country: a speed threshold demotes, never removes the
    option nearest the time asked (synthetic times, 80 km by sea)."""

    ZONE = "Europe/Oslo"
    DEADLINE = datetime(2026, 12, 7, 10, 0, tzinfo=ZoneInfo(ZONE))
    NOW = datetime(2026, 12, 6, 18, 0, tzinfo=timezone.utc)

    def itineraries(self, *rows: dict[str, Any]) -> list[dict[str, Any]]:
        return tp.parse({"itineraries": list(rows)}, ZoneInfo(self.ZONE))

    def test_a_slow_ferry_alone_near_the_time_stays(self) -> None:
        # Ferry 06:40 -> 10:10 (3 h 30, 23 km/h), express boat 11:30 -> 13:00.
        ferry = row(
            "FERRY", "Ferry 12", "2026-12-07T05:40Z", "2026-12-07T09:10Z", self.ZONE
        )
        express = row(
            "FERRY", "Express 3", "2026-12-07T10:30Z", "2026-12-07T12:00Z", self.ZONE
        )
        kept = tp.usable(self.itineraries(ferry, express), 80, self.DEADLINE)
        self.assertEqual(
            [i["legs"][0]["line"] for i in kept], ["Ferry 12", "Express 3"]
        )
        self.assertTrue(kept[0]["long"])
        self.assertNotIn("long", kept[1])

    def test_a_slow_option_with_a_faster_one_near_it_goes(self) -> None:
        # Bus 06:30 -> 10:05 (3 h 35), train 08:50 -> 10:20 (1 h 30): 15 min apart.
        bus = row("BUS", "Bus 400", "2026-12-07T05:30Z", "2026-12-07T09:05Z", self.ZONE)
        train = row(
            "RAIL", "Train R20", "2026-12-07T07:50Z", "2026-12-07T09:20Z", self.ZONE
        )
        kept = tp.usable(self.itineraries(bus, train), 80, self.DEADLINE)
        self.assertEqual([i["legs"][0]["line"] for i in kept], ["Train R20"])

    def test_the_searches_give_the_long_option_then_a_faster_one(self) -> None:
        ferry = row(
            "FERRY", "Ferry 12", "2026-12-07T05:40Z", "2026-12-07T09:10Z", self.ZONE
        )
        express = row(
            "FERRY", "Express 3", "2026-12-07T10:30Z", "2026-12-07T12:00Z", self.ZONE
        )
        answers = [
            {"itineraries": [ferry]},
            {"itineraries": [ferry]},
            {"itineraries": [ferry, express]},
        ]

        class Client:
            config = SimpleNamespace(timeout=8)

            async def plan(self, *_: Any, **__: Any) -> dict[str, Any]:
                return answers.pop(0)

        found = asyncio.run(
            tp._reasonable(
                Client(),  # type: ignore[arg-type]
                ((59.9, 10.7), (59.3, 10.4)),
                self.DEADLINE,
                deadline=self.DEADLINE,
                now=self.NOW,
                zone=ZoneInfo(self.ZONE),
                distance_km=80,
                language="en",
                flights=False,
            )
        )
        assert found is not None
        itineraries, late = found
        self.assertTrue(late)
        self.assertEqual(
            [i["legs"][0]["line"] for i in itineraries], ["Ferry 12", "Express 3"]
        )
        config = SimpleNamespace(
            attribution_name="Transitous",
            attribution_url=tp.SOURCES_URL,
        )
        block, _ = tp.render(
            itineraries,
            origin="your position",
            destination="Harbour Terminal",
            deadline=self.DEADLINE,
            language="en",
            config=config,  # type: ignore[arg-type]
        )
        self.assertIn(
            "- Long trip: 3 h 30 min; no faster itinerary found arrives at a "
            "comparable time.\n- Arrives 10:10, 10 min after the 10:00 requested.",
            block,
        )
