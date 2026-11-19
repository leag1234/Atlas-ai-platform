"""Trip answers on staging -116 (2026-11-18, seven trips on a phone).

Sydney: "Take the T8 Airport & South Line train, which runs directly to Sydney
Airport, stopping ..." went whole and the answer never named the train,
although the page read gave the T8 with Sydney Airport: only "directly" was
written by no page next to them. Madrid: "tomar un tren AVE desde Madrid
Atocha" went because the timetable read wrote "AVE 03183", never "tren AVE".
The consulted-sources list held every search hit (an airport shuttle of
another country, a network switch). From a position at Tokyo station, the
place given to the answer was written in Japanese for an English answer.
Streets are fictional; source sentences are short timetable facts.
"""

import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator import route_facts, travel_claims, travel_rebuild
from services.orchestrator.location import Position
from services.orchestrator.nearby import OSM, place_in_script

LINE = "https://wiki.example.test/T8_Airport_and_South_Line"
GUIDE = "https://guide.example.test/Sydney_Airport"
TIMETABLE = "https://rail.example.test/ave-03183-madrid-atocha-barcelona-sants"

LEAD = (
    "The T8 Airport & South Line is a suburban rail service operated by Sydney "
    "Trains, connecting the Sydney central business district with the "
    "southwestern suburbs via Sydney Airport."
)
STATIONS = (
    "Sydney Trains have two stations at Sydney Airport. Domestic station serves "
    "T2 and T3, and International station serves T1. The airport is on the T8 "
    "Airport line and trains run at least every 10 minutes between 0600 and 2200."
)
SYDNEY_ASKED = (
    "how do i get to the airport tomorrow by public transport, need to be there by 8am"
)
SYDNEY_PLACE = "Wattle Lane, Sydney NSW 2000, Australia"
SYDNEY = (
    "Take the T8 Airport & South Line train, which runs directly to Sydney "
    "Airport, stopping at both the Domestic and International terminals."
)

AVE = (
    "AVE 03183 Horaires train Madrid Atocha-Barcelona Sants. En opération entre "
    "Madrid Atocha et Barcelona Sants, le AVE 03183 démarre son trajet à Gare "
    "Madrid Atocha à 18:30 h et arrive à Gare Barcelona Sants à 21:20 h après "
    "2 heures 50 minutes."
)
MADRID_ASKED = (
    "mañana tengo que estar en barcelona sants antes de las 13:00, qué tren cojo?"
)
MADRID_PLACE = "Calle del Olmo 5, 28012 Madrid, España"
MADRID = (
    "Para llegar a Barcelona Sants mañana antes de las 13:00, debes tomar un "
    "tren AVE desde Madrid Atocha, ya que la estación de origen es Madrid Puerta "
    "de Atocha. El trayecto dura unas 2 horas 50 minutos. Los trenes AVE de esa "
    "franja suelen ser directos."
)


def turn(
    asked: str, place: str, language: str, *pages: dict[str, str]
) -> travel_claims.Turn:
    found = travel_claims.Turn(asked=[asked], language=language)
    found.located(place=place)
    for page in pages:
        found.collect(page)
    return found


def body(answer: str) -> str:
    return answer.split("\n\n")[0]


class SydneyTests(unittest.TestCase):
    def test_only_the_unwritten_direct_goes(self) -> None:
        trip = turn(
            SYDNEY_ASKED,
            SYDNEY_PLACE,
            "en",
            {"link": LINE, "title": "T8 Airport & South Line", "snippet": LEAD},
            {"link": GUIDE, "title": "Sydney Airport", "text": STATIONS},
        )
        checked = trip.check(SYDNEY, "en")
        self.assertIn(
            "Take the T8 Airport & South Line train, which runs to Sydney Airport, "
            "stopping at both the Domestic and International terminals.",
            body(checked),
        )
        self.assertIn("Not verified, so left out: “directly”.", checked)

    def test_a_closing_clause_no_page_gives_goes_with_it(self) -> None:
        trip = turn(
            SYDNEY_ASKED,
            SYDNEY_PLACE,
            "en",
            {"link": LINE, "title": "T8 Airport & South Line", "snippet": LEAD},
        )
        checked = trip.check(SYDNEY, "en")
        self.assertIn(
            "Take the T8 Airport & South Line train, which runs to Sydney Airport.",
            body(checked),
        )
        self.assertNotIn("International", body(checked))
        self.assertIn("“stopping at both the Domestic and International", checked)

    def test_a_line_no_page_gives_still_takes_its_sentence(self) -> None:
        trip = turn(
            SYDNEY_ASKED,
            SYDNEY_PLACE,
            "en",
            {"link": LINE, "title": "T8 Airport & South Line", "snippet": LEAD},
        )
        answer = "Take the T4 train, which runs directly to Sydney Airport."
        found = trip.findings(answer)
        self.assertEqual(
            [(s, e) for s, e, _ in found.route.spans], [(0, len(answer) - 1)]
        )
        self.assertFalse(found.route.exact)
        # Nothing of the sentence stays: the answer is the note alone.
        self.assertTrue(
            trip.check(answer, "en").startswith(
                "Not verified, so left out: “Take the T4"
            )
        )

    def test_a_direct_the_sentence_states_is_not_cut_alone(self) -> None:
        trip = turn(
            SYDNEY_ASKED,
            SYDNEY_PLACE,
            "en",
            {"link": LINE, "title": "T8 Airport & South Line", "snippet": LEAD},
        )
        answer = "From Central, the T8 to Sydney Airport is direct. Buy an Opal card."
        checked = trip.check(answer, "en")
        self.assertTrue(body(checked).startswith("Buy an Opal card."), checked)

    def test_a_cut_never_leaves_only_the_request(self) -> None:
        trip = turn(
            SYDNEY_ASKED,
            SYDNEY_PLACE,
            "en",
            {"link": LINE, "title": "T8 Airport & South Line", "snippet": LEAD},
        )
        answer = "To reach Sydney Airport by 8am, take the T4 to Bondi Junction."
        self.assertFalse(trip.findings(answer).route.exact)


class MadridTests(unittest.TestCase):
    def test_a_service_written_with_its_train_number_sources_the_service(
        self,
    ) -> None:
        trip = turn(
            MADRID_ASKED,
            MADRID_PLACE,
            "es",
            {"link": TIMETABLE, "title": "AVE 03183", "snippet": AVE},
        )
        checked = trip.check(MADRID, "es")
        self.assertIn("debes tomar un tren AVE desde Madrid Atocha", body(checked))
        self.assertIn("2 horas 50 minutos", body(checked))
        # No page says the trains of that slot are direct: still left out.
        self.assertNotIn("directos", body(checked))
        self.assertIn("“Los trenes AVE de esa franja suelen ser directos”", checked)

    def test_one_or_two_letters_still_need_their_transit_word(self) -> None:
        (pattern,) = route_facts._line_patterns("b")
        self.assertIsNone(pattern.search("plan b for the trip"))
        self.assertIsNotNone(pattern.search("rer b to the airport"))
        (pattern,) = route_facts._line_patterns("ave")
        self.assertIsNotNone(pattern.search("ave 03183 madrid atocha"))


class SourceListTests(unittest.TestCase):
    def test_hits_about_no_place_of_the_trip_are_not_listed(self) -> None:
        shuttle = "https://shuttle.example.test/gatwick"
        switch = "https://switch.example.test/lynx-t8"
        bridge = "https://wiki.example.test/Sydney_Harbour_Bridge"
        trip = turn(
            SYDNEY_ASKED,
            SYDNEY_PLACE,
            "en",
            {"link": LINE, "title": "T8 Airport & South Line", "snippet": LEAD},
            {
                "link": shuttle,
                "title": "Gatwick airport shuttle",
                "snippet": "Shuttle buses to Gatwick airport every 20 minutes.",
            },
            {
                "link": switch,
                "title": "Lynx T8 switch",
                "snippet": "Layer 3 Ethernet switch with 8 ports.",
            },
            {
                "link": bridge,
                "title": "Sydney Harbour Bridge",
                "snippet": "A steel arch bridge across Sydney Harbour.",
            },
        )
        self.assertEqual(
            travel_rebuild.unrelated(trip, SYDNEY_ASKED, SYDNEY),
            {shuttle, switch, bridge},
        )

    def test_a_turn_without_trip_words_hides_nothing(self) -> None:
        trip = turn(
            "and then?",
            "",
            "en",
            {"link": LINE, "title": "T8", "snippet": LEAD},
        )
        self.assertEqual(travel_rebuild.unrelated(trip, "and then?", ""), set())


class PlaceScriptTests(unittest.IsolatedAsyncioTestCase):
    def test_a_part_in_another_script_goes_for_a_latin_script_language(
        self,
    ) -> None:
        parts = ["JR東京駅;10番線", "Marunouchi 1", "100-0005 Chiyoda", "Japan"]
        self.assertEqual(
            place_in_script(parts, "en"), "Marunouchi 1, 100-0005 Chiyoda, Japan"
        )
        self.assertEqual(place_in_script(parts, "ja"), ", ".join(parts))
        # Nothing in the person's script: the place stays as the map gives it.
        self.assertEqual(
            place_in_script(["東京駅", "千代田区"], "en"), "東京駅, 千代田区"
        )

    async def test_the_map_service_is_asked_in_the_person_language(self) -> None:
        osm = OSM()
        answer = {
            "address": {
                "road": "JR東京駅;10番線",
                "quarter": "Marunouchi",
                "city": "Chiyoda",
                "postcode": "100-0005",
                "country": "Japan",
            }
        }
        with patch.object(osm, "request", AsyncMock(return_value=answer)) as call:
            place = await osm.reverse(Position(latitude=35.68, longitude=139.76), "en")
        self.assertEqual(place, "Marunouchi, 100-0005 Chiyoda, Japan")
        assert call.await_args is not None
        self.assertEqual(call.await_args.args[1]["accept-language"], "en")

    async def test_reverse_geocoding_keeps_the_place_names_it_read(self) -> None:
        osm = OSM()
        answer = {
            "address": {
                "road": "Blumenweg",
                "suburb": "Neuhausen",
                "city": "München",
                "postcode": "80636",
                "country": "Germany",
            },
            "namedetails": {"name": "München", "name:en": "Munich"},
        }
        with patch.object(osm, "request", AsyncMock(return_value=answer)) as call:
            place = await osm.reverse(Position(latitude=48.15, longitude=11.53), "en")
        self.assertEqual(place, "Blumenweg, Neuhausen, 80636 München, Germany")
        self.assertEqual(osm.place_names, ["München", "Munich"])
        assert call.await_args is not None
        self.assertEqual(call.await_args.args[1]["namedetails"], "1")


if __name__ == "__main__":
    unittest.main()
