"""A named airport or station ends the trip at its stop, however the name is
written (staging -138).

The battery's Geneva trip reached the planner as "Aéroport de Genève (GVA),
Genève, Suisse". The geocoder put the aerodrome first (its point amid the
runways, area Meyrin) and the station Genève-Aéroport second; the stop rule
of -135 only gave way to a town, an area or an address, so the plans ended
at "aéroport de Genève (Meyrin)", a 15-minute walk from a bus stop
(09:14-09:15) instead of the airport station by train (09:05). The wording
"aéroport de Genève (Meyrin)" put four bus stops named after the airport's
buildings after the aerodrome, the station not among them.

The first match now gives way to a stop when it is the transport place
itself as the geocoder maps it (an aerodrome, a flight stop when the person
does not fly) or a business named after it, and one stop search (cached)
looks for the stop when the first results hold none. Recorded answers:
tests/fixtures/transit/named-places-qualifiers.json (the staging -138
planner cache, and api.transitous.org on 2026-11-20 for other wordings and
other countries); every place there is public.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from services.orchestrator import transit_planner as tp
from test_named_transport_place import GEOCODES, HOME, NOW, STATION_ID, place
from test_transit_planner import Base, recorded

QUALIFIED = {
    row["trip"]: row
    for row in json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "transit"
            / "named-places-qualifiers.json"
        ).read_text()
    )["geocodes"]
}
STAGING = QUALIFIED["lyon-geneva-staging-138"]
# The stop searches (type STOP) by their text.
STOP_SEARCHES = {
    row["params"]["text"]: row
    for trip, row in QUALIFIED.items()
    if row["params"].get("type") == "STOP"
}
SYDNEY_T3 = "au-nsw-Transport-for-New-South-Wales-GSBC009_202067"


class Geocoder:
    """A fake MOTIS server answering from the recorded geocodes."""

    def __init__(
        self, rows: dict[str, dict[str, Any]], plans: dict[str, str] | None = None
    ) -> None:
        self.rows = rows
        self.plans = plans or {}
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, path: str, params: dict[str, str]) -> tuple[int, bytes]:
        self.calls.append((path, params))
        if path == "/api/v1/geocode":
            if params.get("type") == "STOP":
                row = STOP_SEARCHES.get(params["text"])
                return 200, json.dumps(row["matches"] if row else []).encode()
            row = self.rows.get(params["text"])
            return 200, json.dumps(row["matches"] if row else []).encode()
        if path == "/api/v1/reverse-geocode":
            return 200, b"[]"
        name = self.plans.get(params["toPlace"] + " " + params["time"], "")
        body = recorded(name) if name else {"itineraries": [], "direct": []}
        return 200, json.dumps(body).encode()

    def stop_searches(self) -> list[dict[str, str]]:
        return [p for path, p in self.calls if p.get("type") == "STOP"]

    def plan_calls(self) -> list[dict[str, str]]:
        return [p for path, p in self.calls if path.endswith("/plan")]


def by_text(*rows: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["params"]["text"]: row for row in rows}


class RecordedWordingsTest(Base):
    def chosen(self, row: dict[str, Any]) -> tuple[dict[str, Any], Geocoder]:
        geocoder = Geocoder(by_text(row))
        client = tp.Client(self.config(), geocoder)

        async def run() -> dict[str, Any]:
            matches = await client.geocode(row["params"]["text"], "fr", HOME)
            first = tp.choose(matches, biased=False, named=row["params"]["text"])
            return await tp._named_place(
                client,
                row["params"]["text"],
                matches,
                first,
                language="fr",
                flights=False,
            )

        return asyncio.run(run()), geocoder

    def test_each_wording_ends_at_the_airport_or_station_stop(self) -> None:
        expected = {
            # Geneva, as the battery's model wrote it (-138, -135) and as a
            # person would: the station of the airport.
            "lyon-geneva-staging-138": ("Genève-Aéroport", ""),
            "geneva-meyrin": ("Genève-Aéroport", "aéroport de Genève"),
            "lyon-geneva-staging-135": ("Genève-Aéroport", ""),
            # The geocoder's first stop stays (an airport coach station).
            "geneva-en": ("Geneva Airport (Bus Station)", ""),
            "zurich-airport": ("Zürich Flughafen, Bahnhof", ""),
            "schiphol": ("Amsterdam Schiphol Airport", ""),
            "vienna-station": ("Wien Hauptbahnhof", ""),
            # The aerodrome first, no stop in the first results.
            "madrid-airport": (
                "Madrid - Barajas Airport T4",
                "Aeropuerto Adolfo Suárez Madrid-Barajas",
            ),
            # A car rental desk named after the airport first.
            "heathrow": ("London Heathrow Airport Terminal 5", ""),
            # The airport's flight stop first: the person does not fly.
            "malpensa": (
                "Malpensa aeroporto terminal 1",
                "Aeroporto di Milano Malpensa",
            ),
        }
        for trip, (name, searched) in expected.items():
            row = QUALIFIED.get(trip) or GEOCODES[trip]
            with self.subTest(trip=trip):
                target, geocoder = self.chosen(row)
                self.assertEqual(target["name"], name)
                self.assertEqual(target["type"], "STOP")
                self.assertEqual(
                    [p["text"] for p in geocoder.stop_searches()],
                    [searched] if searched else [],
                )

    def test_the_battery_trips_of_minus_138_keep_their_ends(self) -> None:
        # Stations the geocoder puts first, and a district named without a
        # transport word: no stop search, the first match.
        for trip in (
            "caen-paris",
            "caen-paris-bare",
            "munich-berlin",
            "new-york-boston",
            "tokyo-kyoto",
            "madrid-barcelona",
            "lyon-geneva",
        ):
            row = GEOCODES[trip]
            with self.subTest(trip=trip):
                target, geocoder = self.chosen(row)
                self.assertEqual(target["name"], row["matches"][0]["name"])
                self.assertEqual(geocoder.stop_searches(), [])


class TripTest(Base):
    def plan(
        self, destination: str, geocoder: Geocoder, **request: Any
    ) -> dict[str, Any]:
        config = self.config()
        return asyncio.run(
            tp.compute(
                tp.TransitPlan(
                    destination=destination,
                    from_device_position=True,
                    arrive_by="2026-11-21T09:00",
                    **request,
                ),
                config=config,
                language="fr",
                position=HOME,
                home_zone="Europe/Paris",
                days=["2026-11-21"],
                now=NOW,
                client=tp.Client(config, geocoder),
            )
        )

    GENEVA_PLANS = {
        STATION_ID + " 2026-11-21T09:00+01:00": "geneva-airport-stop",
        STATION_ID + " 2026-11-21T09:30+01:00": "geneva-airport-stop-late",
    }

    def test_the_minus_138_trip_ends_at_the_station_by_train(self) -> None:
        geocoder = Geocoder(by_text(STAGING), self.GENEVA_PLANS)
        data = self.plan(STAGING["params"]["text"], geocoder)
        self.assertEqual({p["toPlace"] for p in geocoder.plan_calls()}, {STATION_ID})
        self.assertEqual(geocoder.stop_searches(), [])
        self.assertEqual(data["outcome"], "planned_late")
        self.assertTrue(data["destination"].startswith("Genève-Aéroport"))
        rendered = data["rendered"]
        self.assertIn("Arrivée 09:05, 5 min après", rendered)
        self.assertNotIn("(Meyrin)", rendered)
        # Every itinerary rides into the airport station's own stops (the
        # recorded plans: train to Vernier, then the bus to the station's
        # Arena stop), never a walk to the aerodrome's point.
        for itinerary in data["itineraries"]:
            last = itinerary["legs"][-1]
            self.assertNotEqual(last["kind"], "walk")
            self.assertTrue(last["to"].startswith("Genève-Aéroport"))
            self.assertIn("train", {leg["kind"] for leg in itinerary["legs"]})

    def test_the_meyrin_wording_finds_the_station_by_one_stop_search(self) -> None:
        row = QUALIFIED["geneva-meyrin"]
        geocoder = Geocoder(by_text(row), self.GENEVA_PLANS)
        data = self.plan(row["params"]["text"], geocoder)
        searches = geocoder.stop_searches()
        self.assertEqual(len(searches), 1)
        # The aerodrome's own name, around the aerodrome: the search the
        # snapping of an unreached airport makes, so one cache entry.
        self.assertEqual(searches[0]["text"], "aéroport de Genève")
        self.assertEqual(searches[0]["place"], "46.2261,6.0870")
        self.assertEqual({p["toPlace"] for p in geocoder.plan_calls()}, {STATION_ID})
        self.assertIn("Arrivée 09:05, 5 min après", data["rendered"])

    def test_a_failed_stop_search_keeps_the_first_match(self) -> None:
        row = QUALIFIED["geneva-meyrin"]
        geocoder = Geocoder(by_text(row))

        async def failing(path: str, params: dict[str, str]) -> tuple[int, bytes]:
            if params.get("type") == "STOP":
                return 503, b""
            return await geocoder(path, params)

        config = self.config()
        client = tp.Client(config, failing)

        async def run() -> dict[str, Any]:
            matches = await client.geocode(row["params"]["text"], "fr", HOME)
            return await tp._named_place(
                client,
                row["params"]["text"],
                matches,
                matches[0],
                language="fr",
                flights=False,
            )

        self.assertEqual(asyncio.run(run())["name"], "aéroport de Genève")

    def test_the_sydney_airport_trip_ends_at_the_stop_found_before(self) -> None:
        # -138 ended at Sydney Airport, Terminal 3 Domestic after a plan to
        # the aerodrome found no stop at its point; the same stop is now the
        # end from the start, by the same (cached) stop search.
        row = GEOCODES["sydney-airport"]
        geocoder = Geocoder(by_text(row))
        config = self.config()
        client = tp.Client(config, geocoder)

        async def run() -> dict[str, Any]:
            matches = await client.geocode("Sydney Airport", "en", (-33.8688, 151.2093))
            return await tp._named_place(
                client,
                "Sydney Airport",
                matches,
                matches[0],
                language="en",
                flights=False,
            )

        target = asyncio.run(run())
        self.assertEqual(target["id"], SYDNEY_T3)
        search = geocoder.stop_searches()[0]
        self.assertEqual(
            {k: search[k] for k in ("text", "place")},
            {"text": "Sydney Airport", "place": "-33.9463,151.1881"},
        )


class RulesTest(Base):
    AERODROME = place(
        "aéroport de Genève", 46.2261, 6.0870, category="aerodrome", id="way/1"
    )
    STATION = place(
        "Genève-Aéroport",
        46.2325,
        6.1120,
        "STOP",
        id=STATION_ID,
        modes=["LONG_DISTANCE", "REGIONAL_RAIL"],
    )
    ICC = place("Genève-Aéroport, ICC", 46.2263, 6.1014, "STOP", modes=["BUS"])
    FLIGHTS = place(
        "Genève Aéroport (GVA)", 46.2379, 6.1091, "STOP", modes=["AIRPLANE"]
    )

    def chosen(self, named: str, matches: list[dict[str, Any]], **options: Any) -> str:
        return str(tp.choose(matches, biased=True, named=named, **options)["name"])

    def test_qualifiers_order_and_accents_do_not_matter(self) -> None:
        matches = [self.AERODROME, self.ICC, self.STATION]
        for named in (
            "Aéroport de Genève (GVA), Le Grand-Saconnex, Suisse",
            "aéroport de Genève (Meyrin)",
            "Aéroport international de Genève-Cointrin",
            "Geneva Airport, Switzerland",
            "Genève, aéroport",
            "aeroport de geneve terminal",
            "Flughafen Genf",
            # A code alone, when the geocoder maps it to the aerodrome: the
            # aerodrome's own words name the stop.
            "GVA",
        ):
            with self.subTest(named=named):
                self.assertEqual(self.chosen(named, matches), "Genève-Aéroport")

    def test_the_first_match_stays_when_it_is_the_place_asked(self) -> None:
        cases = {
            # A hotel whose every word is a word of the name asked.
            "Hôtel de la Gare, Genève": [
                place("Hôtel de la Gare", 46.2100, 6.1400, category="hotel_16"),
                self.STATION,
            ],
            # An airport stop the geocoder puts first.
            "Geneva Airport": [self.STATION, self.ICC],
            # An aerodrome without a stop named after it.
            "aéroport de Genève": [self.AERODROME, self.ICC],
            # A stop farther than 40 km from the aerodrome.
            "aéroport de Genève (GVA)": [
                self.AERODROME,
                {**self.STATION, "lat": 46.7, "lon": 6.6},
            ],
        }
        for named, matches in cases.items():
            with self.subTest(named=named):
                self.assertIs(tp.choose(matches, biased=True, named=named), matches[0])

    def test_a_business_named_after_the_airport_gives_way(self) -> None:
        rental = place(
            "Exemple Cars Genève Aéroport", 46.2300, 6.1000, category="rental_car_16"
        )
        self.assertEqual(
            self.chosen("Aéroport de Genève", [rental, self.ICC, self.STATION]),
            "Genève-Aéroport",
        )

    def test_the_flight_stop_stays_only_for_a_flight(self) -> None:
        matches = [self.FLIGHTS, self.STATION]
        named = "aéroport de Genève (GVA)"
        self.assertEqual(self.chosen(named, matches), "Genève-Aéroport")
        self.assertEqual(
            self.chosen(named, matches, flights=True), "Genève Aéroport (GVA)"
        )

    def test_no_stop_search_without_a_transport_place(self) -> None:
        geocoder = Geocoder({})
        client = tp.Client(self.config(), geocoder)
        town = place("Sants", 41.3753, 2.1349, category="place_capital_8")
        hotel = place("Mercure Sydney Airport", -33.93, 151.16, category="hotel_16")
        port = place("Port Melbourne", -37.84, 144.94, category="suburb")

        async def run(named: str, first: dict[str, Any]) -> dict[str, Any]:
            return await tp._named_place(
                client, named, [first], first, language="en", flights=False
            )

        for named, first in (
            ("Barcelona Sants", town),
            ("Mercure Sydney Airport", hotel),
            ("Port Melbourne", port),
            ("Geneva Airport", self.STATION),
        ):
            with self.subTest(named=named):
                self.assertIs(asyncio.run(run(named, first)), first)
        self.assertEqual(geocoder.calls, [])

    def test_the_core_name_drops_qualifiers(self) -> None:
        self.assertEqual(
            tp.core_name("Aéroport de Genève (GVA), Le Grand-Saconnex, Suisse"),
            "Aéroport de Genève",
        )
        self.assertEqual(tp.core_name("Gare de Lyon"), "Gare de Lyon")
        self.assertEqual(tp.core_name("(GVA)"), "(GVA)")
