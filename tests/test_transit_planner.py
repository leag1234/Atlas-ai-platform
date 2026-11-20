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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import aiohttp

from packages import privacy
from services.orchestrator import transit_planner as tp
from services.orchestrator import relative_dates, travel_claims
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
    "Boston South Station": [
        match("Boston (South Station)", 42.3505, -71.0562, "America/New_York", "Boston")
    ],
    "Geneva Airport": [
        match("Geneve-Aeroport", 46.2325, 6.112, "Europe/Zurich", "Le Grand-Saconnex")
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


# Reverse geocoding by type: the start's town for a doubtful trip's search
# (a fictional street of a public town).
REVERSE: dict[str, list[dict[str, Any]]] = {
    "ADDRESS": [
        match(
            "Rue Exemple", 45.769, 4.882, "Europe/Paris", "Villeurbanne", kind="ADDRESS"
        )
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
        if path == "/api/v1/reverse-geocode":
            return 200, json.dumps(REVERSE.get(params["type"], [])).encode()
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
        days: list[str] | None = None,
        depart_at: str | None = None,
    ) -> dict[str, Any]:
        config = self.config()
        request = tp.TransitPlan(
            destination=destination,
            origin=origin,
            from_device_position=position is not None,
            arrive_by=arrive_by,
            depart_at=depart_at,
        )
        return asyncio.run(
            tp.compute(
                request,
                config=config,
                language=language,
                position=position,
                home_zone="Europe/Paris",
                days=days or [],
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


# --- Staging -128 (2026-11-19, UI trip battery on the planner) ---------------


class NamesTest(Base):
    def test_kana_service_names_are_romanised_with_the_original(self) -> None:
        self.assertEqual(
            tp.service_name("\u306e\u305e\u307f11\u53f7"),
            "Nozomi 11 (\u306e\u305e\u307f11\u53f7)",
        )
        self.assertEqual(tp.service_name("ICE 802"), "ICE 802")
        self.assertEqual(tp.romanised("\u304d\u3063\u3077"), "kippu")
        self.assertEqual(tp.romanised("\u3068\u3046\u304d\u3087\u3046"), "toukyou")
        self.assertEqual(tp.romanised("\u30b7\u30e3\u30c8\u30eb"), "shatoru")

    def test_all_caps_stop_names_are_written_as_names(self) -> None:
        self.assertEqual(tp.place_name("TOKYO"), "Tokyo")
        self.assertEqual(tp.place_name("NY PENN STATION"), "Ny Penn Station")
        self.assertEqual(tp.place_name("7 AV/W 34 ST"), "7 AV/W 34 ST")
        self.assertEqual(tp.place_name("Gare de Lyon"), "Gare de Lyon")
        self.assertEqual(tp.place_name("MONDEVILLE Centre"), "MONDEVILLE Centre")

    def test_tokyo_plan_in_english_reads_without_japanese_alone(self) -> None:
        data = self.compute(
            Planner("tokyo"),
            "Kyoto Station",
            position=(35.681, 139.767),
            arrive_by="2026-11-20T10:00",
        )
        rendered = data["rendered"]
        self.assertIn(
            "Train Nozomi 11 (\u306e\u305e\u307f11\u53f7) towards Hakata: Tokyo 07:30",
            rendered,
        )
        self.assertNotIn("TOKYO", rendered)
        self.assertNotIn("KYOTO", rendered)


class DateTest(Base):
    def test_the_day_the_person_named_wins_over_the_call(self) -> None:
        # Staging -128: "tmrw" asked, the call gave today, every train was late.
        planner = Planner("new-york")
        data = self.compute(
            planner,
            "Boston South Station",
            position=(40.751, -73.992),
            arrive_by="2026-11-19T11:00",
            days=["2026-11-20"],
        )
        _, params = planner.calls[-1]
        self.assertEqual(params["time"], "2026-11-20T11:00-05:00")
        self.assertEqual(data["day"], "2026-11-20")
        self.assertEqual(data["outcome"], "planned")
        self.assertIn("Arrives 10:25 for 11:00 requested.", data["rendered"])

    def test_chat_spellings_of_tomorrow_name_the_day(self) -> None:
        tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
        for word in ("tmrw", "tomorow", "tommorow", "tomorrow"):
            with self.subTest(word=word):
                self.assertIn(
                    tomorrow,
                    travel_claims.user_dates(
                        [f"i gotta be at the station {word} by 11am"], "UTC"
                    ),
                )
        self.assertTrue(
            relative_dates.mentions_relative_date("boston south station tmrw by 11am")
        )


class AnswerFactsTest(Base):
    def depart_turn(self) -> tuple[travel_claims.Turn, dict[str, Any]]:
        planner = Planner("new-york-depart")

        async def fetch(
            _self: Any, path: str, params: dict[str, str]
        ) -> tuple[int, bytes]:
            return await planner(path, params)

        turn = travel_claims.Turn(
            active=True,
            asked=["whats the best train from here to Boston South Station?"],
            zone="America/New_York",
            position=Position(latitude=40.751, longitude=-73.992),
        )
        result = self.run_tool(
            {
                "destination": "Boston South Station",
                "from_device_position": True,
                "depart_at": "2026-11-20T07:30",
            },
            fetch,
            turn,
        )
        return turn, result["data"]

    def test_the_operator_of_a_planned_train_is_a_fact_of_the_plan(self) -> None:
        turn, data = self.depart_turn()
        self.assertTrue(turn.planned)
        sentence = (
            "The earliest option is Amtrak Acela 2150, which leaves Ny Moynihan "
            "Train Hall At Penn Station at 07:57 and arrives at Boston at 11:44."
        )
        checked = turn.check(data["attribution"] + "\n\n" + sentence, "en")
        self.assertIn(sentence, checked)
        self.assertNotIn("Not verified", checked)

    def test_after_a_plan_the_note_never_says_atlas_computes_no_route(self) -> None:
        turn, data = self.depart_turn()
        answer = (
            data["rendered"]
            + "\n\nThen take the Red Line towards Alewife from South Station at 12:10."
        )
        for language, clause in (
            ("en", "nor do the itineraries computed for it"),
            ("fr", "ni les itin\u00e9raires calcul\u00e9s pour elle"),
        ):
            with self.subTest(language=language):
                checked = turn.check(answer, language)
                self.assertNotIn("Alewife", checked.partition("\n\nN")[0])
                self.assertIn(clause, checked)
                self.assertNotIn("does not compute routes", checked)
                self.assertNotIn("ne calcule pas encore", checked)

    def test_without_a_planner_the_note_still_says_so(self) -> None:
        turn = travel_claims.Turn(active=True, asked=["train to Boston?"])
        with patch.dict(os.environ, {"ATLAS_TRANSIT": "0"}):
            checked = turn.check("Take the Red Line towards Alewife at 12:10.", "en")
        self.assertIn("does not compute routes yet", checked)

    def test_a_planner_without_a_plan_never_says_atlas_computes_no_route(
        self,
    ) -> None:
        # Staging -129 (Sydney): the planner found nothing and the note said
        # "Atlas does not compute routes yet".
        turn = travel_claims.Turn(active=True, asked=["train to the airport?"])
        for language, clause in (
            ("en", "no computed itinerary was found for this trip"),
            (
                "fr",
                "aucun itin\u00e9raire calcul\u00e9 n\u2019a \u00e9t\u00e9 trouv\u00e9",
            ),
            ("de", "keine berechnete Verbindung gefunden"),
        ):
            with self.subTest(language=language):
                checked = turn.check(
                    "Take the Red Line towards Alewife at 12:10.", language
                )
                self.assertIn(clause, checked)
                self.assertNotIn("does not compute routes", checked)
                self.assertNotIn("ne calcule pas encore", checked)
                self.assertNotIn("noch keine Routen", checked)

    def test_the_instruction_asks_for_the_second_person(self) -> None:
        _, data = self.depart_turn()
        self.assertIn("second person", data["instruction"])
        self.assertIn("never in their own first-person words", data["instruction"])
        self.assertIn("second person", tp.DESCRIPTION)


# The seven trips of the staging battery: start, destination, deadline.
BATTERY = {
    "caen": ((49.181, -0.364), (48.8446, 2.3751), "Europe/Paris", 12, 30),
    "munich": ((48.1542, 11.5315), (52.525, 13.3692), "Europe/Berlin", 14, 0),
    "lyon": ((45.769, 4.882), (46.2325, 6.112), "Europe/Zurich", 9, 0),
    "new-york": ((40.7508, -73.9923), (42.3505, -71.0562), "America/New_York", 11, 0),
    "tokyo": ((35.681, 139.767), (34.9842, 135.7593), "Asia/Tokyo", 10, 0),
    "sydney": ((-33.869, 151.21), (-33.9463, 151.1881), "Australia/Sydney", 7, 0),
    "madrid": ((40.4178, -3.699), (41.3753, 2.1349), "Europe/Madrid", 13, 0),
}


class DoubtTest(Base):
    def test_only_the_night_coach_chain_of_the_battery_is_doubtful(self) -> None:
        early = datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc)
        for name, (start, end, zone, hour, minute) in BATTERY.items():
            with self.subTest(trip=name):
                deadline = datetime(2026, 11, 20, hour, minute, tzinfo=ZoneInfo(zone))
                kept, _ = tp.selected(
                    tp.parse(recorded(name), ZoneInfo(zone)),
                    now=early,
                    deadline=deadline,
                )
                found = tp.doubts(kept, tp._distance_km(start, end), deadline)
                self.assertEqual(bool(found), name == "lyon", found)

    def test_a_doubtful_plan_is_a_fallback_with_a_caution(self) -> None:
        data = self.compute(
            Planner("lyon"),
            "Geneva Airport",
            position=(45.769, 4.882),
            arrive_by="2026-11-20T09:00",
            language="fr",
            now=datetime(2026, 11, 19, 18, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(data["outcome"], "doubtful")
        self.assertEqual(set(data["doubts"]), {"slow", "night"})
        self.assertIn(
            "_Attention : l\u2019itin\u00e9raire le plus rapide", data["rendered"]
        )
        self.assertIn("partent la nuit", data["rendered"])
        self.assertIn("fallback", data["next_step"])
        self.assertIn("never as the answer", data["next_step"])

    def test_a_plausible_plan_has_no_caution(self) -> None:
        data = self.compute(
            Planner("munich"),
            "Berlin Hauptbahnhof",
            origin="Lindenweg 4, Munich",
            arrive_by="2026-11-20T14:00",
        )
        self.assertEqual(data["outcome"], "planned")
        self.assertNotIn("doubts", data)
        self.assertNotIn("Caution", data["rendered"])


class SnapTest(Base):
    """Staging -129: "King Street, Sydney" geocoded to a street point the
    planner's walk search could not attach (0 start offsets): no itinerary."""

    KING_STREET = match(
        "King Street",
        -33.8688,
        151.2036,
        "Australia/Sydney",
        "City of Sydney",
        kind="ADDRESS",
    )
    STOP = {
        **match("Town Hall", -33.8732, 151.2066, "Australia/Sydney", "City of Sydney"),
        "id": "AU-NSW_200060",
    }

    def planner(self) -> Planner:
        planner = Planner("sydney")

        async def fake(path: str, params: dict[str, str]) -> tuple[int, bytes]:
            planner.calls.append((path, params))
            # The origin is the unreached end; an arrive-by search runs
            # backwards from the destination, so its "dest" offsets are the
            # origin's (recorded from api.transitous.org on 2026-11-19).
            side = (
                "n_dest_offsets"
                if params.get("arriveBy") == "true"
                else "n_start_offsets"
            )
            unreached = {
                "debugOutput": {"n_start_offsets": 167, "n_dest_offsets": 167, side: 0},
                "itineraries": [],
                "direct": [],
            }
            if path == "/api/v1/geocode":
                places = {"King Street, Sydney": [self.KING_STREET], **PLACES}
                return 200, json.dumps(places.get(params["text"], [])).encode()
            if path == "/api/v1/reverse-geocode":
                return 200, json.dumps([self.STOP]).encode()
            if params["fromPlace"] == self.STOP["id"]:
                return 200, json.dumps(recorded("sydney-stop")).encode()
            return 200, json.dumps(unreached).encode()

        planner.fake = fake  # type: ignore[attr-defined]
        return planner

    def test_an_unreached_start_is_planned_from_its_nearest_stop(self) -> None:
        planner = self.planner()
        config = self.config()
        data = asyncio.run(
            tp.compute(
                tp.TransitPlan(
                    destination="Sydney Airport",
                    origin="King Street, Sydney",
                    arrive_by="2026-11-20T07:00",
                ),
                config=config,
                language="en",
                position=None,
                home_zone="Australia/Sydney",
                days=["2026-11-20"],
                now=datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc),
                client=tp.Client(config, planner.fake),  # type: ignore[attr-defined]
            )
        )
        self.assertEqual(data["outcome"], "planned")
        self.assertEqual(planner.plans(), 2)
        second = [p for path, p in planner.calls if path.endswith("/plan")][1]
        self.assertEqual(second["fromPlace"], "AU-NSW_200060")
        self.assertIn(
            "The planner reaches no stop from King Street (City of Sydney) itself: "
            "the plan starts at Town Hall (City of Sydney), the nearest stop "
            "(0.6 km in a straight line).",
            data["rendered"],
        )

    def test_a_far_stop_is_never_taken_for_the_place(self) -> None:
        planner = self.planner()
        self.STOP_FAR = {**self.STOP, "lat": -33.95, "lon": 151.3}
        config = self.config()

        async def far(path: str, params: dict[str, str]) -> tuple[int, bytes]:
            if path == "/api/v1/reverse-geocode":
                return 200, json.dumps([self.STOP_FAR]).encode()
            answer: tuple[int, bytes] = await planner.fake(path, params)  # type: ignore[attr-defined]
            return answer

        data = asyncio.run(
            tp.compute(
                tp.TransitPlan(
                    destination="Sydney Airport", origin="King Street, Sydney"
                ),
                config=config,
                language="en",
                position=None,
                home_zone="Australia/Sydney",
                days=[],
                now=NOW,
                client=tp.Client(config, far),
            )
        )
        self.assertEqual(data["outcome"], "no_itinerary")
        # One more search over a wider window, never from the far stop.
        self.assertEqual(planner.plans(), 2)
        for path, params in planner.calls:
            if path.endswith("/plan"):
                self.assertEqual(params["fromPlace"], "-33.86880,151.20360")

    def test_an_unreached_airport_ends_at_its_terminal_stop(self) -> None:
        """Staging -131: the Sydney Airport point lies amid the runways, with
        no stop within reach (0 offsets on the destination's side of an
        arrive-by search); the start was snapped instead and the retry found
        nothing again. The plan now ends at the nearest stop bearing the
        airport's name."""
        planner = Planner("sydney")
        airport = match(
            "Sydney Airport",
            -33.9463,
            151.1881,
            "Australia/Sydney",
            "Bayside",
            kind="PLACE",
        )
        terminal = {
            **match(
                "Sydney Airport, Terminal 3 Domestic",
                -33.9334,
                151.1806,
                "Australia/Sydney",
                "Bayside",
            ),
            "id": "AU-NSW_202067",
        }
        unreached = {
            "debugOutput": {"n_start_offsets": 0, "n_dest_offsets": 167},
            "itineraries": [],
            "direct": [],
        }

        async def fake(path: str, params: dict[str, str]) -> tuple[int, bytes]:
            planner.calls.append((path, params))
            if path == "/api/v1/geocode":
                if params.get("type") == "STOP":
                    far = {**terminal, "name": "Sydney Airport Hotel", "lat": -34.2}
                    return 200, json.dumps([far, terminal]).encode()
                places = {
                    "King Street, Sydney": [self.KING_STREET],
                    "Sydney Airport": [airport],
                }
                return 200, json.dumps(places.get(params["text"], [])).encode()
            if path == "/api/v1/reverse-geocode":
                near_street = params["place"].startswith("-33.86")
                return 200, json.dumps([self.STOP] if near_street else []).encode()
            if params["toPlace"] == terminal["id"]:
                return 200, json.dumps(recorded("sydney-stop")).encode()
            return 200, json.dumps(unreached).encode()

        config = self.config()
        data = asyncio.run(
            tp.compute(
                tp.TransitPlan(
                    destination="Sydney Airport",
                    origin="King Street, Sydney",
                    arrive_by="2026-11-20T07:00",
                ),
                config=config,
                language="en",
                position=None,
                home_zone="Australia/Sydney",
                days=["2026-11-20"],
                now=datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc),
                client=tp.Client(config, fake),
            )
        )
        self.assertEqual(data["outcome"], "planned")
        second = [p for path, p in planner.calls if path.endswith("/plan")][1]
        self.assertEqual(second["toPlace"], "AU-NSW_202067")
        self.assertEqual(second["fromPlace"], "-33.86880,151.20360")
        self.assertIn(
            "The planner reaches no stop at Sydney Airport (Bayside) itself: the "
            "plan ends at Sydney Airport, Terminal 3 Domestic (Bayside), the "
            "nearest stop found (1.6 km in a straight line).",
            data["rendered"],
        )


# The staging -129 answer to "Villeurbanne Gratte-Ciel to Geneva airport by
# 09:00": the night chain via Annecy given as the most direct option.
NIGHT_ANSWER = (
    "Pour \u00eatre \u00e0 l\u2019a\u00e9roport de Gen\u00e8ve le vendredi 20 novembre "
    "2026 \u00e0 9 h, les itin\u00e9raires calcul\u00e9s en transports en commun "
    "partent tous la nuit, d\u00e8s 03:04, et arrivent entre 08:18 et 08:27. "
    "Voici l\u2019option la plus directe parmi celles trouv\u00e9es :\n\n"
    "- Bus PL4 direction Cordeliers : Verlaine - Tolsto\u00ef 03:19 \u2192 Saxe - "
    "Lafayette 03:29 ([transitous.org](https://transitous.org/sources/))\n"
    "- Car FlixBus N779 direction Chamonix : Lyon (Gerland Bus Station) 05:05 "
    "\u2192 Annecy (Central Bus Station) 06:40 "
    "([transitous.org](https://transitous.org/sources/))\n\n"
    "Itin\u00e9raires calcul\u00e9s avec [Transitous](https://transitous.org/sources/) "
    "\u00e0 partir des donn\u00e9es ouvertes des exploitants."
)


class DoubtfulAnswerTest(Base):
    def doubtful_turn(self) -> tuple[travel_claims.Turn, dict[str, Any]]:
        planner = Planner("lyon")

        async def fetch(
            _self: Any, path: str, params: dict[str, str]
        ) -> tuple[int, bytes]:
            return await planner(path, params)

        turn = travel_claims.Turn(
            active=True,
            language="fr",
            asked=[
                "je dois \u00eatre \u00e0 l'a\u00e9roport de Gen\u00e8ve demain \u00e0 9h"
            ],
            zone="Europe/Paris",
            position=Position(latitude=45.769, longitude=4.882),
        )
        token = travel_claims.current.set(turn)
        try:
            with (
                patch.object(tp.Client, "_fetch", fetch),
                patch.object(
                    tp,
                    "_now",
                    lambda: datetime(2026, 11, 19, 18, 0, tzinfo=timezone.utc),
                ),
            ):
                result = asyncio.run(
                    tp.execute(
                        json.dumps(
                            {
                                "destination": "Geneva Airport",
                                "from_device_position": True,
                                "arrive_by": "2026-11-20T09:00",
                            }
                        ),
                        30.0,
                    )
                )
        finally:
            travel_claims.current.reset(token)
        data = result["data"]
        assert isinstance(data, dict)
        return turn, data

    def answer(self, turn: travel_claims.Turn, text: str) -> str:
        token = travel_claims.current.set(turn)
        try:
            return tp.doubtful_answer(text, "fr")
        finally:
            travel_claims.current.reset(token)

    def test_the_plan_is_titled_a_fallback_and_the_search_names_the_town(
        self,
    ) -> None:
        turn, data = self.doubtful_turn()
        self.assertEqual(data["outcome"], "doubtful")
        self.assertTrue(
            data["rendered"].startswith("**Solution de repli seulement, calcul\u00e9e")
        )
        self.assertEqual(
            data["direct_query"],
            "Villeurbanne Geneve-Aeroport train ou car direct horaires",
        )
        self.assertEqual(turn.doubtful, data["rendered"])
        self.assertIn("partent la nuit", turn.doubt_reason)

    def test_a_night_chain_answer_says_no_reasonable_connection_first(self) -> None:
        turn, _ = self.doubtful_turn()
        turn.direct_pages = ["https://www.example-rail.test/lyon-geneve"]
        final = self.answer(turn, NIGHT_ANSWER)
        self.assertTrue(
            final.startswith("Aucune liaison raisonnable en transports en commun"),
            final,
        )
        self.assertIn(
            "[example-rail.test](https://www.example-rail.test/lyon-geneve)", final
        )
        self.assertNotIn("option la plus directe", final)
        self.assertNotIn("d\u00e8s 03:04, et arrivent", final)
        self.assertIn(turn.doubtful, final)
        self.assertEqual(final.count("Solution de repli seulement"), 1)

    def test_an_answer_that_leads_with_the_pages_keeps_them_first(self) -> None:
        turn, data = self.doubtful_turn()
        lead = (
            "Le TER direct Lyon Part-Dieu \u2013 Gen\u00e8ve part \u00e0 06:42 et arrive "
            "\u00e0 08:32 ([example-rail.test](https://www.example-rail.test/lyon-geneve))."
        )
        text = (
            lead
            + "\n\nEn repli :\n\n"
            + data["rendered"]
            + "\n\n<details>Sources</details>"
        )
        final = self.answer(turn, text)
        self.assertTrue(final.startswith(lead), final)
        self.assertEqual(final.count("Solution de repli seulement"), 1)
        self.assertNotIn("En repli :", final)
        self.assertTrue(final.endswith("<details>Sources</details>"))
        self.assertLess(final.index(lead), final.index("Solution de repli"))

    def test_a_plausible_plan_answer_is_left_as_written(self) -> None:
        turn = travel_claims.Turn(active=True)
        self.assertEqual(self.answer(turn, NIGHT_ANSWER), NIGHT_ANSWER)

    def test_the_pages_read_put_the_direct_services_first(self) -> None:
        turn, data = self.doubtful_turn()
        token = travel_claims.current.set(turn)
        try:
            read = tp.with_direct_pages(
                data, [{"url": "https://www.example-rail.test/a", "text": "x" * 5000}]
            )
            unread = tp.with_direct_pages(data, [])
        finally:
            travel_claims.current.reset(token)
        self.assertEqual(read["next_step"], tp.NEXT_STEPS["doubtful_read"])
        self.assertEqual(len(read["direct_services"][0]["text"]), tp.DIRECT_CHARACTERS)
        self.assertIn("direct_services", read["instruction"])
        self.assertEqual(unread["next_step"], tp.NEXT_STEPS["doubtful_unread"])
        self.assertIn("no reasonable connection", unread["next_step"])
        self.assertEqual(turn.direct_pages, [])

    def test_the_planner_pages_are_never_read_as_direct_services(self) -> None:
        results = {
            "results": [
                {"link": "https://transitous.org/sources/"},
                {"link": "https://www.example-rail.test/a"},
                {"link": "https://www.example-rail.test/a"},
                {"link": "https://www.example-coach.test/b"},
                {"link": "https://www.example-coach.test/c"},
            ]
        }
        self.assertEqual(
            tp.direct_candidates(results),
            ["https://www.example-rail.test/a", "https://www.example-coach.test/b"],
        )
        self.assertEqual(tp.direct_candidates({"error": "x"}), [])


class DirectServicesTest(Base):
    """A doubtful plan makes code search and read the operators' direct
    services through the turn's own web tools (staging -129)."""

    def run_plan(self, search: dict[str, Any]) -> tuple[dict[str, Any], Any, list[str]]:
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Call
        from services.orchestrator.tools import Runtime

        calls: list[str] = []
        plan = {
            "outcome": "doubtful",
            "rendered": "**Solution de repli seulement: a \u2192 b**",
            "source_urls": [tp.SOURCES_URL, tp.OSM_URL],
            "doubts": {"night": {"depart": "03:04"}},
            "direct_query": "Villeurbanne Geneve-Aeroport train ou car direct horaires",
        }

        async def fake(_self: Any, call: Any, timeout: float) -> dict[str, Any]:
            arguments = json.loads(call.arguments)
            calls.append(
                f"{call.name}:{arguments.get('url') or arguments.get('query', '')}"
            )
            if call.name == "transit_plan":
                return {"data": dict(plan)}
            if call.name == "web_search":
                return search
            return {"data": {"url": arguments["url"], "text": "TER direct 06:42."}}

        turn = travel_claims.Turn(active=True, language="fr")
        token = travel_claims.current.set(turn)
        try:
            with (
                patch.object(Runtime, "execute", fake),
                patch.dict(
                    os.environ, {"ATLAS_WEB_CACHE": self.state.name + "/web.sqlite"}
                ),
            ):
                tools = ChatTools(Interaction())
                result = asyncio.run(
                    tools.execute(Call("c1", "transit_plan", "{}"), 30.0)
                )
        finally:
            travel_claims.current.reset(token)
        data = result["data"]
        assert isinstance(data, dict)
        return data, tools, calls

    def test_the_first_pages_found_are_read_and_put_first(self) -> None:
        data, tools, calls = self.run_plan(
            {
                "data": {
                    "results": [
                        {"link": tp.SOURCES_URL, "snippet": "x"},
                        {"link": "https://www.example-rail.test/a", "snippet": "x"},
                        {"link": "https://www.example-coach.test/b", "snippet": "x"},
                        {"link": "https://www.example-coach.test/c", "snippet": "x"},
                    ]
                }
            }
        )
        self.assertEqual(
            calls,
            [
                "transit_plan:",
                "web_search:Villeurbanne Geneve-Aeroport train ou car direct horaires",
                "web_fetch:https://www.example-rail.test/a",
                "web_fetch:https://www.example-coach.test/b",
            ],
        )
        self.assertEqual(
            [page["url"] for page in data["direct_services"]],
            ["https://www.example-rail.test/a", "https://www.example-coach.test/b"],
        )
        self.assertEqual(data["next_step"], tp.NEXT_STEPS["doubtful_read"])
        self.assertIn("https://www.example-rail.test/a", tools.item.retrieved_urls)

    def test_a_failed_search_says_no_page_was_read(self) -> None:
        data, _, calls = self.run_plan({"error": "search_unavailable"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(data["direct_services"], [])
        self.assertEqual(data["next_step"], tp.NEXT_STEPS["doubtful_unread"])
