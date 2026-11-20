"""A trip ends at the transport place the person names (staging -135).

The battery's Geneva trip ("I must be at Geneva airport tomorrow at 9")
reached the planner as "Aéroport de Genève (GVA), Le Grand-Saconnex, Suisse".
The geocoder put the town Le Grand-Saconnex first and the station
Genève-Aéroport fourth: the plans ended in the town (09:08) instead of at the
airport, whose station and stops the planner reaches at 09:05.

When the name holds a transport word (airport, station, bus station, port,
terminal, in several languages and scripts) and the geocoder puts a town, an
area or an address first, a stop of the results already fetched stands for
the place: its name carries a word of that kind (or its modes serve it), its
other words are words of the name asked, and it lies within 10 km of the
first match. Recorded answers: tests/fixtures/transit/named-places-geocode.json
(the staging planner cache for -135; api.transitous.org on 2026-11-20 for the
other destinations of the battery). Other places here are public; their
points are rounded; the address is fictional.
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.orchestrator import transit_planner as tp
from test_transit_planner import Base, recorded

GEOCODES = {
    row["trip"]: row
    for row in json.loads(
        (
            Path(__file__).parent / "fixtures" / "transit" / "named-places-geocode.json"
        ).read_text()
    )["geocodes"]
}
STAGING = GEOCODES["lyon-geneva-staging-135"]
STATION_ID = "ch-opentransportdataswiss26_Parentch:1:sloid:1026"
# The battery ran at 04:50 in Villeurbanne, the day before the trip.
NOW = datetime(2026, 11, 20, 3, 50, tzinfo=timezone.utc)
HOME = (45.77, 4.88)


def place(
    name: str, lat: float, lon: float, kind: str = "PLACE", **extra: Any
) -> dict[str, Any]:
    return {"type": kind, "name": name, "lat": lat, "lon": lon, **extra}


def chosen(named: str, matches: list[dict[str, Any]], **options: Any) -> str:
    return str(tp.choose(matches, biased=True, named=named, **options)["name"])


class RecordedTest(Base):
    def test_the_airport_station_stands_for_the_town_put_first(self) -> None:
        matches = STAGING["matches"]
        self.assertEqual(matches[0]["name"], "Le Grand-Saconnex")
        self.assertEqual(matches[3]["name"], "Genève-Aéroport")
        target = tp.choose(matches, biased=False, named=STAGING["params"]["text"])
        self.assertIs(target, matches[3])
        # Never the town's bus stops, though their words are words of the name.
        self.assertIsNone(
            tp.named_stop("Le Grand-Saconnex, Suisse", matches),
        )

    def test_the_battery_destinations_keep_their_first_match(self) -> None:
        # Stations and airports the geocoder already puts first; "Barcelona
        # Sants" names no transport word: the district stays.
        for trip, row in GEOCODES.items():
            if trip == STAGING["trip"]:
                continue
            with self.subTest(trip=trip):
                matches = row["matches"]
                target = tp.choose(matches, biased=False, named=row["params"]["text"])
                self.assertIs(target, matches[0])

    def test_the_trip_ends_at_the_airport_not_in_the_town(self) -> None:
        plans: list[dict[str, str]] = []
        recordings = {
            "2026-11-21T09:00+01:00": "geneva-airport-stop",
            "2026-11-21T09:30+01:00": "geneva-airport-stop-late",
        }

        async def planner(path: str, params: dict[str, str]) -> tuple[int, bytes]:
            if path == "/api/v1/geocode":
                return 200, json.dumps(STAGING["matches"]).encode()
            if path == "/api/v1/reverse-geocode":
                return 200, b"[]"
            plans.append(params)
            name = recordings[params["time"]] if params["toPlace"] == STATION_ID else ""
            body = recorded(name) if name else {"itineraries": [], "direct": []}
            return 200, json.dumps(body).encode()

        config = self.config()
        data = self.run_compute(planner, config)
        self.assertEqual([p["toPlace"] for p in plans], [STATION_ID, STATION_ID])
        self.assertEqual(data["outcome"], "planned_late")
        rendered = data["rendered"]
        self.assertIn("Genève-Aéroport", rendered)
        self.assertIn("Arrivée 09:05, 5 min après", rendered)
        self.assertNotIn("09:08", rendered)

    def run_compute(self, planner: Any, config: tp.Settings) -> dict[str, Any]:
        return asyncio.run(
            tp.compute(
                tp.TransitPlan(
                    destination=STAGING["params"]["text"],
                    from_device_position=True,
                    arrive_by="2026-11-21T09:00",
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

    def test_a_named_station_of_origin_starts_the_plan(self) -> None:
        plans: list[dict[str, str]] = []
        caen = [
            place("Caen", 49.1829, -0.3707, category="city"),
            place("Caen", 49.1766, -0.3484, "STOP", id="fr-sncf:caen", modes=["RAIL"]),
        ]

        async def planner(path: str, params: dict[str, str]) -> tuple[int, bytes]:
            if path == "/api/v1/geocode":
                if params["text"] == "gare de Caen":
                    return 200, json.dumps(caen).encode()
                return 200, json.dumps(GEOCODES["caen-paris"]["matches"]).encode()
            if path == "/api/v1/reverse-geocode":
                return 200, b"[]"
            plans.append(params)
            return 200, json.dumps(recorded("caen")).encode()

        config = self.config()
        asyncio.run(
            tp.compute(
                tp.TransitPlan(
                    destination="Gare de Lyon, Paris", origin="gare de Caen"
                ),
                config=config,
                language="fr",
                position=None,
                home_zone="Europe/Paris",
                days=[],
                now=NOW,
                client=tp.Client(config, planner),
            )
        )
        self.assertEqual(plans[0]["fromPlace"], "fr-sncf:caen")
        # The Paris station the geocoder put first stays a point.
        self.assertEqual(plans[0]["toPlace"], "48.84457,2.37515")


class WordsTest(Base):
    def test_transport_words_of_several_languages(self) -> None:
        cases = {
            "Flughafen München, Freising": [
                place("Freising", 48.4029, 11.7488, category="town"),
                place("Freising", 48.3953, 11.7454, "STOP", modes=["REGIONAL_RAIL"]),
                place(
                    "München Flughafen Terminal",
                    48.3536,
                    11.7854,
                    "STOP",
                    modes=["SUBURBAN"],
                ),
            ],
            "Aeropuerto de Madrid-Barajas": [
                place("Barajas", 40.4703, -3.5800, category="place_suburb_10"),
                place(
                    "Madrid-Barajas Aeropuerto T4",
                    40.4919,
                    -3.5933,
                    "STOP",
                    modes=["SUBWAY"],
                ),
            ],
            "stazione di Bologna Centrale": [
                place("Bologna", 44.4938, 11.3426, category="city"),
                place(
                    "Bologna Centrale",
                    44.5058,
                    11.3431,
                    "STOP",
                    modes=["HIGHSPEED_RAIL"],
                ),
            ],
            "gare routière de Lyon Perrache": [
                place("Lyon", 45.7578, 4.8320, category="city"),
                place("Lyon Perrache", 45.7485, 4.8258, "STOP", modes=["RAIL"]),
                place("Perrache Gare Routière", 45.7489, 4.8262, "STOP", modes=["BUS"]),
            ],
            "Geneva Airport": [
                place("Le Grand-Saconnex", 46.2363, 6.1265, category="town"),
                place(
                    "Genève-Aéroport", 46.2325, 6.1120, "STOP", modes=["REGIONAL_RAIL"]
                ),
            ],
            "京都駅": [  # Kyoto's station, as written in Japanese
                place("京都市", 35.0116, 135.7681, category="city"),
                place("京都", 34.9858, 135.7588, "STOP", modes=["RAIL"]),
            ],
            "aeroporto do Porto": [
                place("Porto", 41.1496, -8.6110, category="city"),
                place("Aeroporto", 41.2372, -8.6700, "STOP", modes=["SUBWAY"]),
                place("Porto Aeroporto", 41.2370, -8.6699, "STOP", modes=["SUBWAY"]),
            ],
            "Dover ferry port": [
                place("Dover", 51.1279, 1.3134, category="town"),
                place("Dover Port", 51.1268, 1.3317, "STOP", modes=["FERRY"]),
            ],
        }
        expected = {
            "Flughafen München, Freising": "München Flughafen Terminal",
            "Aeropuerto de Madrid-Barajas": "Madrid-Barajas Aeropuerto T4",
            "stazione di Bologna Centrale": "Bologna Centrale",
            "gare routière de Lyon Perrache": "Perrache Gare Routière",
            "Geneva Airport": "Genève-Aéroport",
            "京都駅": "京都",
            "Dover ferry port": "Dover Port",
            "aeroporto do Porto": "Porto Aeroporto",
        }
        for named, matches in cases.items():
            with self.subTest(named=named):
                self.assertEqual(chosen(named, matches), expected[named])

    def test_the_first_match_stays_when_unsure(self) -> None:
        cases = {
            # A town whose own name holds the word is that town.
            "Port Melbourne": [
                place("Port Melbourne", -37.8396, 144.9423, category="suburb"),
                place("Port Melbourne", -37.8300, 144.9300, "STOP", modes=["TRAM"]),
            ],
            # A stop farther than 10 km from the town is another place, an
            # airport farther than 40 km too.
            "aéroport de Caen": [
                place("Caen", 49.1829, -0.3707, category="city"),
                place("Caen Aéroport", 49.6000, -0.3700, "STOP", modes=["BUS"]),
            ],
            # A station farther than 10 km.
            "gare de Caen": [
                place("Caen", 49.1829, -0.3707, category="city"),
                place("Caen", 49.3000, -0.5000, "STOP", modes=["RAIL"]),
            ],
            # A hotel, a shop, an aerodrome: the place itself, not a town.
            "Mercure Sydney Airport": [
                place(
                    "Mercure Sydney Airport", -33.9345, 151.1572, category="hotel_16"
                ),
                place("Sydney Airport", -33.9350, 151.1650, "STOP", modes=["BUS"]),
            ],
            # A street named after the station is that street.
            "9 rue de la Gare, Exempleville": [
                place("9 Rue de la Gare", 49.1771, -0.3497, "ADDRESS"),
                place("Caen", 49.1766, -0.3484, "STOP", modes=["RAIL"]),
            ],
            # No transport word.
            "Barcelona Sants": [
                place("Sants", 41.3753, 2.1349, category="place_capital_8"),
                place("Barcelona-Sants", 41.3794, 2.1406, "STOP", modes=["RAIL"]),
            ],
            # A stop whose name says more than the name asked.
            "aéroport de Genève": [
                place("Le Grand-Saconnex", 46.2363, 6.1265, category="town"),
                place("Genève-Aéroport, ICC", 46.2263, 6.1014, "STOP", modes=["BUS"]),
            ],
            # A station named in the town without the kind asked.
            "Caen airport": [
                place("Caen", 49.1829, -0.3707, category="city"),
                place("Caen", 49.1766, -0.3484, "STOP", modes=["RAIL"]),
            ],
        }
        for named, matches in cases.items():
            with self.subTest(named=named):
                self.assertIs(tp.choose(matches, biased=True, named=named), matches[0])

    def test_a_flight_stop_only_when_the_person_flies(self) -> None:
        matches = [
            place("Le Grand-Saconnex", 46.2363, 6.1265, category="town"),
            place("Genève Aéroport (GVA)", 46.2379, 6.1091, "STOP", modes=["AIRPLANE"]),
        ]
        named = "aéroport de Genève (GVA)"
        self.assertEqual(chosen(named, matches), "Le Grand-Saconnex")
        self.assertEqual(chosen(named, matches, flights=True), "Genève Aéroport (GVA)")

    def test_an_unnamed_choice_is_unchanged(self) -> None:
        matches = STAGING["matches"]
        self.assertIs(tp.choose(matches, biased=False), matches[0])
