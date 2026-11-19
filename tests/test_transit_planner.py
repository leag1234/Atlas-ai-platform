"""Public transport itineraries from a MOTIS 2 journey planner (transit_planner).

Staging -126 (2026-11-18): trip answers built from web pages took a metro in
the wrong direction (Lyon), worked a departure back from a removed duration
(Munich) and proposed a train arriving after the deadline (Madrid). The plan
answers below were recorded from api.transitous.org on 2026-11-19 for trips of
the staging trip battery (tests/fixtures/transit, trimmed to the fields read);
the starting points are coordinates only ("START"). The places geocoded here
are public stations or fictional streets.
"""

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import aiohttp

from packages import privacy
from services.orchestrator import transit_planner as tp
from services.orchestrator import travel_claims
from services.orchestrator.location import Position

FIXTURES = Path(__file__).parent / "fixtures" / "transit"
# Before every recorded departure (the trips are on 2026-11-20).
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def recorded(name: str) -> dict[str, Any]:
    body = json.loads((FIXTURES / f"{name}-plan.json").read_text())["body"]
    assert isinstance(body, dict)
    return body


def match(
    name: str,
    lat: float,
    lon: float,
    zone: str,
    area: str,
    score: float = -20.0,
    kind: str = "STOP",
) -> dict[str, Any]:
    return {
        "type": kind,
        "name": name,
        "lat": lat,
        "lon": lon,
        "tz": zone,
        "score": score,
        "areas": [{"name": area, "default": True}],
    }


# Public stations as the planner's geocoder names them; streets are fictional.
PLACES = {
    "Berlin Hauptbahnhof": [
        match("Berlin Hbf", 52.525, 13.3692, "Europe/Berlin", "Berlin")
    ],
    "Barcelona Sants": [match("Sants", 41.3753, 2.1349, "Europe/Madrid", "Barcelona")],
    "Paris Gare de Lyon": [
        match("Gare de Lyon", 48.8446, 2.3751, "Europe/Paris", "Paris")
    ],
    "Kyoto Station": [match("Kyoto Station", 34.9842, 135.7593, "Asia/Tokyo", "Kyoto")],
    "Sydney Airport": [
        match("Sydney Airport", -33.9463, 151.1881, "Australia/Sydney", "Sydney")
    ],
    "Lindenweg 4, Munich": [
        match(
            "Lindenweg 4", 48.1542, 11.5315, "Europe/Berlin", "Munich", kind="ADDRESS"
        )
    ],
    "Calle Ficticia 9, Madrid": [
        match(
            "Calle Ficticia 9",
            40.4178,
            -3.699,
            "Europe/Madrid",
            "Madrid",
            kind="ADDRESS",
        )
    ],
    # Two streets of the same name in two districts: the geocoder ranks them equal.
    "Example Street": [
        match(
            "Example Street", 40.5811, -74.0007, "America/New_York", "Brooklyn", -29.1
        ),
        match(
            "Example Street", 40.7508, -73.9923, "America/New_York", "Manhattan", -29.1
        ),
    ],
}


class Planner:
    """A fake MOTIS server: geocodes from PLACES, plans from one recording."""

    def __init__(self, plan: str) -> None:
        self.plan = plan
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, path: str, params: dict[str, str]) -> tuple[int, bytes]:
        self.calls.append((path, params))
        if path == "/api/v1/geocode":
            return 200, json.dumps(PLACES.get(params["text"], [])).encode()
        return 200, json.dumps(recorded(self.plan)).encode()

    def plans(self) -> int:
        return sum(1 for path, _ in self.calls if path.endswith("/plan"))


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.state = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "ATLAS_TRANSIT": "1",
                "ATLAS_TRANSIT_STATE": self.state.name,
                "ATLAS_TRANSIT_MIN_INTERVAL": "0",
            },
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.state.cleanup()

    def run_tool(
        self,
        arguments: dict[str, Any],
        fetch: Any,
        turn: travel_claims.Turn | None = None,
    ) -> dict[str, Any]:
        token = travel_claims.current.set(turn)
        try:
            with (
                patch.object(tp.Client, "_fetch", fetch),
                patch.object(tp, "_now", lambda: NOW),
            ):
                result = asyncio.run(tp.execute(json.dumps(arguments), 30.0))
        finally:
            travel_claims.current.reset(token)
        assert isinstance(result, dict)
        return result

    def config(self) -> tp.Settings:
        config = tp.settings()
        assert config is not None
        return config

    def compute(
        self,
        planner: Planner,
        destination: str,
        *,
        origin: str | None = None,
        arrive_by: str | None = None,
        language: str = "en",
        position: tuple[float, float] | None = None,
        now: datetime = NOW,
    ) -> dict[str, Any]:
        config = self.config()
        request = tp.TransitPlan(
            destination=destination,
            origin=origin,
            from_device_position=position is not None,
            arrive_by=arrive_by,
        )
        return asyncio.run(
            tp.compute(
                request,
                config=config,
                language=language,
                position=position,
                home_zone="Europe/Paris",
                days=[],
                now=now,
                client=tp.Client(config, planner),
            )
        )


class SettingsTest(Base):
    def test_off_unless_switched_on_and_default_url(self) -> None:
        with patch.dict(os.environ, {"ATLAS_TRANSIT": "0"}):
            self.assertIsNone(tp.settings())
            self.assertFalse(tp.enabled())
        config = self.config()
        self.assertEqual(config.url, "https://api.transitous.org")
        self.assertEqual(config.attribution_url, "https://transitous.org/sources/")
        self.assertLess(config.daily_plans, 720)

    def test_empty_or_invalid_url_disables_and_own_server_is_kept(self) -> None:
        with patch.dict(os.environ, {"ATLAS_TRANSIT_URL": ""}):
            self.assertIsNone(tp.settings())
        with patch.dict(os.environ, {"ATLAS_TRANSIT_URL": "ftp://motis.example"}):
            self.assertIsNone(tp.settings())
        with patch.dict(
            os.environ, {"ATLAS_TRANSIT_URL": "https://motis.example.org/"}
        ):
            self.assertEqual(self.config().url, "https://motis.example.org")

    def test_user_agent_names_atlas_version_and_public_page(self) -> None:
        with patch.dict(os.environ, {"ATLAS_VERSION": "0.9.1"}):
            self.assertEqual(
                tp.user_agent(),
                "Atlas/0.9.1 (+https://github.com/leag1234/Atlas-ai-platform)",
            )
        with patch.dict(os.environ, {"ATLAS_VERSION": "bad\r\nvalue"}):
            self.assertNotIn("\n", tp.user_agent())

    def test_strict_and_enhanced_levels_never_offer_the_planner(self) -> None:
        self.assertIn(tp.NAME, privacy.STRICT_EXCLUDED_TOOLS)
        self.assertEqual(privacy.tool_capability(tp.NAME), "places_search")
        token = privacy.elevate(privacy.STRICT)
        try:
            self.assertFalse(privacy.tool_allowed(tp.NAME))
        finally:
            privacy.reset(token)


class PlanTest(Base):
    def test_multi_leg_trip_keeps_lines_directions_and_local_times(self) -> None:
        planner = Planner("munich")
        data = self.compute(
            planner,
            "Berlin Hauptbahnhof",
            origin="Lindenweg 4, Munich",
            arrive_by="2026-11-20T14:00",
        )
        self.assertEqual(data["outcome"], "planned")
        self.assertEqual(data["verdict"], "on_time")
        rendered = data["rendered"]
        # Metro line and its direction from the timetable (the Lyon defect).
        self.assertIn(
            "Metro U7 towards Sendlinger Tor: Rotkreuzplatz 07:04 \u2192 "
            "Hauptbahnhof (U, Tram) 07:08",
            rendered,
        )
        self.assertIn(
            "Train ICE 802 towards Hamburg-Altona: M\u00fcnchen Hbf 07:12", rendered
        )
        self.assertIn("(platform 17)", rendered)
        self.assertIn("Arrives 12:09 for 14:00 requested.", rendered)
        self.assertIn("Times are local (Europe/Berlin)", rendered)
        legs = data["itineraries"][0]["legs"]
        self.assertEqual(legs[1]["line"], "U7")
        self.assertEqual(legs[1]["headsign"], "Sendlinger Tor")
        self.assertEqual(legs[1]["depart"], "2026-11-20T07:04+01:00")
        self.assertEqual(data["arrive_by"], "2026-11-20T14:00+01:00")
        # One deadline query: arrive by 14:00, local time of the destination.
        _, params = planner.calls[-1]
        self.assertEqual(params["arriveBy"], "true")
        self.assertEqual(params["time"], "2026-11-20T14:00+01:00")

    def test_deadline_keeps_only_itineraries_that_arrive_in_time(self) -> None:
        # Madrid: the recorded itineraries arrive 11:12, 12:13 and 12:19.
        data = self.compute(
            Planner("madrid"),
            "Barcelona Sants",
            origin="Calle Ficticia 9, Madrid",
            arrive_by="2026-11-20T12:15",
            language="es",
        )
        arrivals = [i["arrive"][11:16] for i in data["itineraries"]]
        self.assertEqual(arrivals, ["11:12", "12:13"])
        self.assertNotIn("12:19", data["rendered"])
        self.assertIn("Llegada 12:13 para las 12:15 pedidas.", data["rendered"])
        self.assertIn("Tren AVE 03081 direcci\u00f3n Barcelona-Sants", data["rendered"])

    def test_no_itinerary_by_the_deadline_is_said_never_a_late_one(self) -> None:
        data = self.compute(
            Planner("madrid"),
            "Barcelona Sants",
            origin="Calle Ficticia 9, Madrid",
            arrive_by="2026-11-20T11:00",
        )
        self.assertEqual(data["outcome"], "none_by_deadline")
        self.assertEqual(data["itineraries"], [])
        self.assertIn("arrives by 2026-11-20 11:00", data["rendered"])
        self.assertNotIn("11:12", data["rendered"])
        self.assertIn("never present a late itinerary", data["next_step"])

    def test_departures_already_gone_are_left_out(self) -> None:
        later = datetime(2026, 11, 20, 6, 30, tzinfo=timezone.utc)  # 07:30 in Munich
        data = self.compute(
            Planner("munich"),
            "Berlin Hauptbahnhof",
            origin="Lindenweg 4, Munich",
            arrive_by="2026-11-20T14:00",
            now=later,
        )
        departures = [i["depart"][11:16] for i in data["itineraries"]]
        self.assertNotIn("06:59", departures)
        self.assertNotIn("07:19", departures)
        self.assertEqual(departures, ["08:19"])

    def test_device_position_french_wording_and_city_lines(self) -> None:
        data = self.compute(
            Planner("caen"),
            "Paris Gare de Lyon",
            arrive_by="12:30",
            language="fr",
            position=(49.181, -0.364),
            now=datetime(2026, 11, 20, 5, 0, tzinfo=timezone.utc),
        )
        rendered = data["rendered"]
        self.assertIn("Marche 5 min de votre position \u00e0 Bellivet", rendered)
        self.assertIn("Bus 1 direction MONDEVILLE Centre Commercial", rendered)
        self.assertIn("M\u00e9tro 14 direction A\u00e9roport d'Orly", rendered)
        self.assertIn(
            "Train TER 3306 : Caen 10:05 \u2192 Paris Saint-Lazare 12:02", rendered
        )
        self.assertIn("Arriv\u00e9e 12:17 pour 12:30 demand\u00e9.", rendered)
        self.assertIn("[Transitous](https://transitous.org/sources/)", rendered)

    def test_times_are_local_to_the_place(self) -> None:
        data = self.compute(
            Planner("tokyo"),
            "Kyoto Station",
            position=(35.681, 139.767),
            arrive_by="2026-11-20T10:00",
        )
        self.assertIn("Times are local (Asia/Tokyo)", data["rendered"])
        first = data["itineraries"][0]
        self.assertTrue(first["arrive"].endswith("+09:00"))
        self.assertLessEqual(first["arrive"][11:16], "10:00")
        self.assertGreaterEqual(first["depart"][11:16], "06:00")

    def test_far_slower_itineraries_are_dropped(self) -> None:
        # Lyon: a 9 h 32 min overnight chain next to 4 h 18 min.
        data = self.compute(
            Planner("lyon"),
            "Paris Gare de Lyon",
            position=(45.769, 4.882),
            arrive_by="2026-11-20T09:00",
            now=datetime(2026, 11, 19, 18, 0, tzinfo=timezone.utc),
        )
        minutes = [i["minutes"] for i in data["itineraries"]]
        self.assertTrue(minutes)
        self.assertLessEqual(max(minutes), min(minutes) * 1.5 + 30)

    def test_ambiguous_place_is_returned_to_the_model(self) -> None:
        with self.assertRaises(tp.TransitError) as raised:
            self.compute(
                Planner("new-york"), "Barcelona Sants", origin="Example Street"
            )
        self.assertEqual(raised.exception.code, "ambiguous_place")
        self.assertIn("Brooklyn", raised.exception.detail)
        self.assertIn("Manhattan", raised.exception.detail)


class ServiceTest(Base):
    def test_disabled_planner_sends_the_trip_to_the_web_path(self) -> None:
        with patch.dict(os.environ, {"ATLAS_TRANSIT": "0"}):
            result = self.run_tool(
                {"destination": "Berlin Hauptbahnhof"}, Planner("munich")
            )
        self.assertEqual(result["error"], "tool_unavailable")
        self.assertIn("web", result["next_step"])

    def test_unreachable_planner_is_an_outcome_with_the_web_path(self) -> None:
        async def down(
            _self: Any, path: str, params: dict[str, str]
        ) -> tuple[int, bytes]:
            raise aiohttp.ClientConnectionError("refused")

        result = self.run_tool(
            {"destination": "Berlin Hauptbahnhof", "origin": "Lindenweg 4, Munich"},
            down,
        )
        self.assertEqual(result["data"]["outcome"], "planner_unavailable")
        self.assertIn("journey_plan", result["data"]["next_step"])

    def test_http_errors_and_rate_limits_are_named(self) -> None:
        async def limited(
            _self: Any, path: str, params: dict[str, str]
        ) -> tuple[int, bytes]:
            return 429, b"{}"

        result = self.run_tool(
            {"destination": "Berlin Hauptbahnhof", "origin": "x y"}, limited
        )
        self.assertEqual(result["data"]["outcome"], "planner_rate_limited")

    def test_no_timetable_data_falls_back_to_the_web(self) -> None:
        planner = Planner("sydney")

        async def fetch(
            _self: Any, path: str, params: dict[str, str]
        ) -> tuple[int, bytes]:
            return await planner(path, params)

        turn = travel_claims.Turn(
            active=True, position=Position(latitude=-33.869, longitude=151.21)
        )
        result = self.run_tool(
            {"destination": "Sydney Airport", "from_device_position": True}, fetch, turn
        )
        self.assertEqual(result["data"]["outcome"], "no_itinerary")
        self.assertNotIn("rendered", result["data"])
        self.assertEqual(turn.computed, [])

    def test_same_query_is_cached_and_the_daily_cap_holds(self) -> None:
        planner = Planner("munich")
        with patch.dict(os.environ, {"ATLAS_TRANSIT_DAILY_PLANS": "1"}):
            for _ in range(2):
                self.compute(
                    planner,
                    "Berlin Hauptbahnhof",
                    origin="Lindenweg 4, Munich",
                    arrive_by="2026-11-20T14:00",
                )
            self.assertEqual(planner.plans(), 1)
            self.assertEqual(sum(1 for p, _ in planner.calls if "geocode" in p), 2)
            with self.assertRaises(tp.TransitError) as raised:
                self.compute(
                    planner,
                    "Berlin Hauptbahnhof",
                    origin="Lindenweg 4, Munich",
                    arrive_by="2026-11-20T13:00",
                )
            self.assertEqual(raised.exception.code, "planner_daily_limit")
            self.assertEqual(planner.plans(), 1)
            self.assertEqual(tp.Client(self.config()).plans_today(), 1)

    def test_daily_limit_is_said_as_a_limit(self) -> None:
        planner = Planner("munich")

        async def fetch(
            _self: Any, path: str, params: dict[str, str]
        ) -> tuple[int, bytes]:
            return await planner(path, params)

        with patch.dict(os.environ, {"ATLAS_TRANSIT_DAILY_PLANS": "0"}):
            result = self.run_tool(
                {"destination": "Berlin Hauptbahnhof", "origin": "Lindenweg 4, Munich"},
                fetch,
            )
        self.assertEqual(result["data"]["outcome"], "planner_daily_limit")
        self.assertIn("daily request limit", result["data"]["message"])
        self.assertEqual(planner.plans(), 0)


class AnswerTest(Base):
    def planned_turn(self) -> tuple[travel_claims.Turn, dict[str, Any]]:
        planner = Planner("munich")

        async def fetch(
            _self: Any, path: str, params: dict[str, str]
        ) -> tuple[int, bytes]:
            return await planner(path, params)

        turn = travel_claims.Turn(
            active=True,
            asked=[
                "How do I get from Lindenweg 4 in Munich to Berlin Hbf by 2pm tomorrow?"
            ],
            zone="Europe/Berlin",
        )
        result = self.run_tool(
            {
                "destination": "Berlin Hauptbahnhof",
                "origin": "Lindenweg 4, Munich",
                "arrive_by": "2026-11-20T14:00",
            },
            fetch,
            turn,
        )
        return turn, result["data"]

    def test_plan_is_recorded_as_computed_facts_and_passages(self) -> None:
        turn, data = self.planned_turn()
        self.assertEqual(turn.computed, [data["rendered"]])
        self.assertTrue(any("U7" in text for text in turn.passages()))
        self.assertEqual(data["source_urls"][0], "https://transitous.org/sources/")

    def test_answer_keeps_the_plan_and_loses_an_invented_direction(self) -> None:
        turn, data = self.planned_turn()
        answer = (
            data["rendered"]
            + "\n\nTake the U7 towards Olympia-Einkaufszentrum from Rotkreuzplatz at 06:40."
        )
        checked = turn.check(answer, "en")
        self.assertIn("Metro U7 towards Sendlinger Tor: Rotkreuzplatz 07:04", checked)
        self.assertIn("Train ICE 802 towards Hamburg-Altona", checked)
        body, _, note = checked.partition("\n\nNot verified")
        self.assertNotIn("Olympia-Einkaufszentrum", body)
        self.assertNotIn("06:40", body)
        self.assertIn("06:40", note)

    def test_attribution_is_added_when_the_answer_drops_it(self) -> None:
        turn, data = self.planned_turn()
        bare = "Take the 07:12 ICE 802 from M\u00fcnchen Hbf.\n\n<details>Sources</details>"
        fixed = tp.with_attribution(bare, turn.computed, "en")
        body, _, fold = fixed.partition("<details>")
        self.assertIn("[Transitous](https://transitous.org/sources/)", body)
        self.assertIn("https://www.openstreetmap.org/copyright", body)
        self.assertEqual(fold, "Sources</details>")
        self.assertEqual(tp.with_attribution(fixed, turn.computed, "en"), fixed)
        self.assertEqual(tp.with_attribution(bare, [], "en"), bare)


class ParseTest(unittest.TestCase):
    def test_line_names_join_route_and_trip_numbers(self) -> None:
        self.assertEqual(
            tp._line({"displayName": "66", "routeLongName": "Northeast Regional"}),
            "Northeast Regional 66",
        )
        self.assertEqual(
            tp._line({"displayName": "ICE 802", "routeShortName": "18"}), "ICE 802"
        )

    def test_cancelled_itineraries_are_never_written(self) -> None:
        body = recorded("munich")
        body["itineraries"][0]["legs"][1]["cancelled"] = True
        found = tp.parse(body, ZoneInfo("Europe/Berlin"))
        self.assertEqual(len(found), len(body["itineraries"]) - 1)


if __name__ == "__main__":
    unittest.main()
