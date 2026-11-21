"""Trip answers of staging -135 (UI trip battery, 2026-11-20 03:44-03:56 UTC).

"I must be at Geneva airport tomorrow at 9, how do I get there from home by
public transport?" from Villeurbanne (Gratte-Ciel), in French: the planner
gave only night coaches (FlixBus, BlaBlaCar) and the answer said that no
reasonable connection was found. Measured on api.transitous.org the same day
(tests/fixtures/transit/geneva-*, trimmed to the fields read):

- the first regional train, TER 96500 Lyon Part-Dieu 06:38 to Geneva
  Cornavin 08:43, is in the open data, and its last leg too: it reaches Le
  Grand-Saconnex at 09:08 and the airport station at 09:05, a few minutes
  after 09:00. Only night coaches arrive in time, so the plan showed them;
  a second search for arrivals up to 30 minutes later finds the train;
- planned to a point of the airport without naming the transit modes, the
  server's default modes include flights: TGV to Brussels and a flight from
  Charleroi, leaving the day before. Without flights that point gets no
  itinerary at all (its only stops are the airport's flight stops).

The starting point is coordinates only ("START"); the clock is patched.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

from services.orchestrator import travel_claims
from services.orchestrator import transit_planner as tp
from services.orchestrator.location import Position
from test_transit_planner import BATTERY, PLACES, Base, Planner, recorded

QUESTION = (
    "je dois etre a l'aeroport de geneve demain a 9h, comment j'y vais depuis "
    "chez moi en transport en commun ?"
)
HOME = (45.77, 4.88)
# The battery ran at 04:50 in Villeurbanne, the day before the trip.
NOW = datetime(2026, 11, 20, 3, 50, tzinfo=timezone.utc)
AREAS = [
    {"name": "Suisse", "adminLevel": 2.0, "default": False},
    {"name": "Genève", "adminLevel": 4.0, "default": False},
    {"name": "Le Grand-Saconnex", "adminLevel": 8.0, "default": True},
]


def place(name: str, lat: float, lon: float, kind: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": kind,
        "name": name,
        "lat": lat,
        "lon": lon,
        "tz": "Europe/Zurich",
        "score": -20.0,
        "areas": AREAS,
        **extra,
    }


# As the planner's geocoder gave them on 2026-11-20.
SACONNEX = place("Le Grand-Saconnex", 46.2363321, 6.126532, "PLACE")
STATION = place(
    "Genève-Aéroport",
    46.232536,
    6.1119843,
    "STOP",
    id="ch-opentransportdataswiss26_Parentch:1:sloid:1026",
    modes=["LONG_DISTANCE", "REGIONAL_RAIL"],
    importance=0.0062,
)
ICC = place(
    "Genève-Aéroport, ICC",
    46.22626,
    6.1013665,
    "STOP",
    id="ch-opentransportdataswiss26_Parentch:1:sloid:93014",
    modes=["BUS"],
    importance=0.00004,
)
# The point of the live check, amid the airport, and a flight stop next to
# it (the airport as the flight timetables name it).
AIRPORT = place("Aéroport de Genève", 46.2381, 6.109, "PLACE")
FLIGHTS = place(
    "Genève Aéroport (GVA)",
    46.2379,
    6.1091,
    "STOP",
    id="flights_GVA",
    modes=["AIRPLANE"],
    importance=0.02,
)


class GenevaPlanner(Planner):
    """The recorded answers of the live check, by destination and time."""

    PLANS = {
        ("46.23633,6.12653", "2026-11-21T09:00+01:00"): "geneva-saconnex",
        ("46.23633,6.12653", "2026-11-21T09:30+01:00"): "geneva-saconnex-late",
        (STATION["id"], "2026-11-21T09:00+01:00"): "geneva-airport-stop",
        (STATION["id"], "2026-11-21T09:30+01:00"): "geneva-airport-stop-late",
    }

    def __init__(self) -> None:
        super().__init__("geneva-saconnex")

    async def __call__(self, path: str, params: dict[str, str]) -> tuple[int, bytes]:
        self.calls.append((path, params))
        if path == "/api/v1/geocode":
            if params.get("type") == "STOP":
                return 200, json.dumps([FLIGHTS, ICC, STATION]).encode()
            places = {
                "aéroport de Genève, Le Grand-Saconnex": [SACONNEX, STATION],
                "Genève Aéroport": [AIRPORT],
            }
            return 200, json.dumps(places.get(params["text"], [])).encode()
        if path == "/api/v1/reverse-geocode":
            return 200, json.dumps([FLIGHTS]).encode()
        if params["toPlace"] == "46.23810,6.10900":
            flights = "AIRPLANE" in params.get("transitModes", "")
            name = "geneva-airport-point" + ("" if flights else "-noair")
            return 200, json.dumps(recorded(name)).encode()
        name = self.PLANS.get((params["toPlace"], params["time"]), "")
        body = recorded(name) if name else {"itineraries": [], "direct": []}
        return 200, json.dumps(body).encode()

    def plan_calls(self) -> list[dict[str, str]]:
        return [params for path, params in self.calls if path.endswith("/plan")]


class GenevaTest(Base):
    def plan(
        self, destination: str, *, flights: bool = False
    ) -> tuple[GenevaPlanner, dict[str, Any]]:
        planner = GenevaPlanner()
        config = self.config()
        data = asyncio.run(
            tp.compute(
                tp.TransitPlan(
                    destination=destination,
                    from_device_position=True,
                    arrive_by="2026-11-21T09:00",
                    flights=flights,
                ),
                config=config,
                language="fr",
                position=HOME,
                home_zone="Europe/Paris",
                days=["2026-11-21"],
                now=NOW,
                client=tp.Client(config, planner),
            )
        )
        return planner, data


class LateTrainTest(GenevaTest):
    def test_the_first_reasonable_train_is_given_with_its_delay(self) -> None:
        planner, data = self.plan("aéroport de Genève, Le Grand-Saconnex")
        self.assertEqual(data["outcome"], "planned_late")
        self.assertEqual(data["verdict"], "after_deadline")
        calls = planner.plan_calls()
        self.assertEqual(
            [p["time"] for p in calls],
            ["2026-11-21T09:00+01:00", "2026-11-21T09:30+01:00"],
        )
        rendered = data["rendered"]
        self.assertTrue(
            rendered.startswith("**Itin\u00e9raire en transports en commun"), rendered
        )
        self.assertIn("TER 96500", rendered)
        self.assertIn("Lyon Part Dieu 06:38 → Genève Cornavin 08:43", rendered)
        # Staging -142: the note says only what holds of the options shown.
        self.assertIn(
            "Aucun itinéraire trouvé n’arrive au plus tard à 09:00 sans voyager de "
            "nuit. Les options ci-dessous arrivent après 09:00 ; la première arrive "
            "à 09:08, 8 min après l’heure demandée.",
            rendered,
        )
        self.assertNotIn("dès 22:24", rendered)
        self.assertIn(
            "Arrivée 09:08, 8 min après l’heure demandée (09:00).",
            rendered,
        )
        for night in ("FlixBus", "BlaBlaCar", "Solution de repli", "Attention"):
            self.assertNotIn(night, rendered)
        self.assertEqual(len(data["itineraries"]), 3)
        self.assertNotIn("doubts", data)
        self.assertIn("partent la nuit", data["on_time_doubt"])
        self.assertEqual(data["next_step"], tp.NEXT_STEPS["planned_late"])

    def test_the_late_plan_is_the_answers_block(self) -> None:
        planner = GenevaPlanner()
        turn = travel_claims.Turn(
            active=True,
            language="fr",
            asked=[QUESTION],
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
        self.assertEqual(turn.plan, data["rendered"])
        self.assertEqual(turn.doubtful, "")
        self.assertTrue({"06:38", "08:43", "09:08"} <= turn.plan_times)
        self.assertIn(("06:21", "09:08"), turn.plan_ends)
        # "8 minutes late" is arithmetic of the plan.
        turn.asked = [QUESTION]
        self.assertIn(8, turn.plan_gaps())

    def test_a_plausible_plan_is_never_searched_again(self) -> None:
        # The good trips of the battery: one plan request each, never a flight.
        early = datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc)
        trips = {
            "caen": "Paris Gare de Lyon",
            "munich": "Berlin Hauptbahnhof",
            "new-york": "Boston South Station",
            "tokyo": "Kyoto Station",
            "madrid": "Barcelona Sants",
        }
        for name, destination in trips.items():
            start, _, zone, hour, minute = BATTERY[name]
            with self.subTest(trip=name):
                self.assertIn(destination, PLACES)
                planner = Planner(name)
                day = datetime(2026, 11, 20, hour, minute, tzinfo=ZoneInfo(zone))
                data = self.compute(
                    planner,
                    destination,
                    position=start,
                    arrive_by=day.strftime("%Y-%m-%dT%H:%M"),
                    now=early,
                )
                self.assertEqual(data["outcome"], "planned")
                self.assertEqual(planner.plans(), 1)
                modes = planner.calls[-1][1]["transitModes"].split(",")
                self.assertNotIn("AIRPLANE", modes)
                self.assertIn("RAIL", modes)

    def test_nothing_reasonable_later_keeps_the_fallback(self) -> None:
        # The widened searches (+30 min, +2 h, the day's departures) find the
        # night coaches again: the doubtful plan stands, titled a fallback.
        planner = Planner("lyon")
        data = self.compute(
            planner,
            "Geneva Airport",
            position=(45.769, 4.882),
            arrive_by="2026-11-20T09:00",
            language="fr",
            now=datetime(2026, 11, 19, 18, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(data["outcome"], "doubtful")
        self.assertEqual(planner.plans(), 4)
        self.assertTrue(data["rendered"].startswith("**Solution de repli seulement"))


class FlightTest(GenevaTest):
    def test_the_airport_point_is_planned_without_flights(self) -> None:
        planner, data = self.plan("Genève Aéroport")
        calls = planner.plan_calls()
        self.assertEqual(len(calls), 3)
        for params in calls:
            self.assertNotIn("AIRPLANE", params["transitModes"].split(","))
        # No itinerary to the point: the plan ends at the airport's station
        # (the nearest stop named after it, never its flight stop), where
        # only night coaches arrive in time; the train arrives at 09:05.
        self.assertEqual(calls[1]["toPlace"], STATION["id"])
        self.assertEqual(calls[2]["toPlace"], STATION["id"])
        self.assertEqual(data["outcome"], "planned_late")
        rendered = data["rendered"]
        self.assertIn("l’itinéraire arrive à Genève-Aéroport", rendered)
        self.assertIn("TER 96500", rendered)
        self.assertIn("Arrivée 09:05, 5 min après", rendered)
        for word in ("CAG", "Charleroi", "Vol ", "GVA"):
            self.assertNotIn(word, rendered)

    def test_a_flight_is_never_part_of_a_plan_unless_asked(self) -> None:
        zone = ZoneInfo("Europe/Zurich")
        body = recorded("geneva-airport-point")
        self.assertEqual(tp.parse(body, zone), [])
        flown = tp.parse(body, zone, flights=True)
        self.assertTrue(flown)
        self.assertTrue(
            all(any(leg["kind"] == "flight" for leg in i["legs"]) for i in flown)
        )

    def test_flights_are_asked_for_only_when_the_person_flies(self) -> None:
        planner, _ = self.plan("Genève Aéroport", flights=True)
        first = planner.plan_calls()[0]
        self.assertIn("AIRPLANE", first["transitModes"].split(","))
        self.assertIn("flights", tp.TransitPlan.model_json_schema()["properties"])
        self.assertFalse(tp.TransitPlan(destination="Geneva").flights)

    def test_a_flight_stop_is_never_where_a_plan_ends(self) -> None:
        self.assertFalse(tp._served(FLIGHTS, flights=False))
        self.assertTrue(tp._served(FLIGHTS, flights=True))
        self.assertTrue(tp._served(STATION, flights=False))
        self.assertTrue(tp._served({"name": "No modes given"}, flights=False))


class NamedStopTest(Base):
    def test_the_words_of_the_place_name_its_stops_in_any_order(self) -> None:
        planner = GenevaPlanner()
        client = tp.Client(self.config(), planner)
        found = asyncio.run(
            client.stops_named("Aéroport de Genève", "fr", (46.2381, 6.109))
        )
        self.assertEqual(
            [stop["name"] for stop in found],
            [
                "Genève Aéroport (GVA)",
                "Genève-Aéroport, ICC",
                "Genève-Aéroport",
            ],
        )
