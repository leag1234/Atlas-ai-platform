"""Trip answers on staging -118 (2026-11-18, seven trips on a phone).

New York - Boston: "takes 7 hours" was cited to a page that gave that time
for another stretch of the line. Sydney: a Brisbane airport train page was
cited for a Sydney fact, and Perth's and Bangkok's airport trains were listed
as sources. Villeurbanne - Geneva: the answer kept no fact and no page. Streets
are fictional; source sentences are short made-up timetable facts.
"""

import unittest

from services.orchestrator import redirects, travel_claims, travel_rebuild

CORRIDOR = "https://rail.example.test/express-train"
LEG = "https://rail.example.test/new-york-boston"
LINE = "https://wiki.example.test/T8_Airport_and_South_Line"
OTHER_CITY = "https://airtrain.example.test/timetable"
TCL = "https://metro.example.test/ligne-a"
SNCF = "https://rail.example.test/lyon-geneve"

BOSTON_ASKED = (
    "hey i gotta be at boston south station tmrw by 11am, whats the best train"
)
BOSTON_PLACE = "West 40th Street, Manhattan, New York, NY 10018, United States"
SYDNEY_ASKED = "how do i get to sydney airport from here tomorow morning, flight at 8am"
SYDNEY_PLACE = "Wattle Lane, Sydney NSW 2000, Australia"
LEAD = (
    "The T8 Airport & South Line is a suburban rail service operated by Sydney "
    "Trains, connecting the Sydney central business district with Sydney Airport."
)
BRISBANE = (
    "Airtrain connects Brisbane Airport with Central and Roma Street in 20 "
    "minutes; the station access fee is included in the fare."
)


def turn(asked: str, place: str, *pages: dict[str, str]) -> travel_claims.Turn:
    found = travel_claims.Turn(asked=[asked], language="en")
    found.located(place=place)
    for page in pages:
        found.collect(page)
    return found


def boston() -> travel_claims.Turn:
    return turn(
        BOSTON_ASKED,
        BOSTON_PLACE,
        {
            "link": CORRIDOR,
            "title": "Express train",
            "snippet": "Express trains run between Washington, D.C. and Boston in "
            "about 7 hours, with several departures daily.",
        },
        {
            "link": LEG,
            "title": "New York to Boston",
            "snippet": "Trains from New York to Boston take about 3 h 40 min.",
        },
    )


class AnotherLegTests(unittest.TestCase):
    def test_a_duration_of_another_leg_supports_nothing(self) -> None:
        answer = (
            "The express train takes 7 hours "
            f"([rail.example.test]({CORRIDOR})) to Boston South Station."
        )
        found = boston().findings(answer)
        self.assertEqual([q.text for q in found.values], ["7 hours"])

    def test_the_duration_of_this_leg_is_kept(self) -> None:
        answer = (
            "From New York, the train to Boston takes about 3 h 40 min "
            f"([rail.example.test]({LEG}))."
        )
        self.assertFalse(boston().findings(answer).values)

    def test_a_value_cited_to_a_page_comes_from_that_page(self) -> None:
        answer = (
            "From New York, the train to Boston takes about 3 h 40 min "
            f"([rail.example.test]({CORRIDOR}))."
        )
        self.assertEqual(
            [q.text for q in boston().findings(answer).values], ["3 h 40 min"]
        )

    def test_a_leg_the_answer_names_is_this_trip(self) -> None:
        answer = (
            "Change in Washington: Washington to Boston takes about 7 hours "
            f"([rail.example.test]({CORRIDOR}))."
        )
        self.assertFalse(boston().findings(answer).values)

    def test_months_are_no_leg(self) -> None:
        places = frozenset({"boston", "york"})
        self.assertFalse(
            travel_claims.another_leg("From January to March, 7 hours.", places)
        )
        self.assertTrue(
            travel_claims.another_leg("From Washington to Boston, 7 hours.", places)
        )


class PlaceNameTests(unittest.TestCase):
    MUNICH_URL = "https://rail.example.test/munich-berlin"
    GENEVA_URL = "https://rail.example.test/lyon-geneva"
    TOKYO_URL = "https://rail.example.test/tokyo-yokohama"
    OTHER_URL = "https://rail.example.test/hamburg-bremen"

    def turn(
        self, asked: str, place: str, names: list[str], url: str, snippet: str
    ) -> travel_claims.Turn:
        found = travel_claims.Turn(asked=[asked])
        found.located(place=place, place_names=names)
        found.collect({"link": url, "title": "Timetable", "snippet": snippet})
        return found

    def test_a_place_written_in_another_language_is_the_same_place(self) -> None:
        asked = "how do i get to berlin hbf tomorrow morning?"
        place = "Blumenweg, Neuhausen, 80636 München, Germany"
        snippet = (
            "The fastest journey from Munich to Berlin takes about 3 hours "
            "and 53 minutes."
        )
        trip = self.turn(asked, place, ["München", "Munich"], self.MUNICH_URL, snippet)
        answer = "The fastest journey takes about 3 hours and 53 minutes."
        self.assertEqual(trip.findings(answer).labels(), [])

    def test_geneva_written_in_english_is_still_the_starting_point(self) -> None:
        asked = "je dois être à l'aéroport de Genève demain matin, je pars de Lyon"
        place = "Rue de la République, 69002 Lyon, France"
        snippet = "Trains from Lyon to Geneva Airport take about 2 hours."
        trip = self.turn(asked, place, ["Genève", "Geneva"], self.GENEVA_URL, snippet)
        answer = "Le trajet prend environ 2 heures."
        self.assertEqual(trip.findings(answer).labels(), [])

    def test_a_non_latin_name_matches_its_latin_name(self) -> None:
        asked = "how do i get to yokohama station tomorrow morning?"
        place = "Marunouchi 1, 100-0005 Chiyoda, Japan"
        snippet = "Trains from 東京 to Yokohama take about 30 minutes."
        trip = self.turn(asked, place, ["東京", "Tokyo"], self.TOKYO_URL, snippet)
        answer = "The journey takes about 30 minutes."
        self.assertEqual(trip.findings(answer).labels(), [])

    def test_a_page_about_another_city_is_still_left_out(self) -> None:
        asked = "how do i get to berlin hbf tomorrow morning?"
        place = "Blumenweg, Neuhausen, 80636 München, Germany"
        snippet = "Trains from Hamburg to Bremen take about 90 minutes."
        trip = self.turn(asked, place, ["München", "Munich"], self.OTHER_URL, snippet)
        answer = "The journey takes about 90 minutes."
        self.assertEqual(
            trip.findings(answer).labels(), ["“The journey takes about 90 minutes”"]
        )


class ReverseDirectionTests(unittest.TestCase):
    SYDNEY_URL = "https://rail.example.test/sydney-airport"
    MADRID_URL = "https://rail.example.test/madrid-barcelona"
    TURIN_URL = "https://rail.example.test/turin-milan"
    LYON_URL = "https://rail.example.test/lyon-marseille"

    def turn(
        self, asked: str, place: str, url: str, snippet: str
    ) -> travel_claims.Turn:
        return turn(
            asked,
            place,
            {"link": url, "title": "Timetable", "snippet": snippet},
            {"link": url, "title": "Timetable", "text": snippet},
        )

    def test_reverse_train_times_are_not_this_trip(self) -> None:
        asked = (
            "how do i get to sydney airport from here tomorow morning, flight at 8am"
        )
        place = "Wattle Lane, Sydney NSW 2000, Australia"
        snippet = (
            "The T8 train from Sydney Airport SYD to City of Sydney runs from "
            "04:18 to 23:53, with departures every 5-15 min."
        )
        trip = self.turn(asked, place, self.SYDNEY_URL, snippet)
        answer = (
            "The T8 train from Sydney Airport SYD to City of Sydney runs from "
            "04:18 to 23:53, with departures every 5-15 min "
            f"([rail.example.test]({self.SYDNEY_URL}))."
        )
        self.assertNotIn("04:18 to 23:53", trip.kept(answer, trip.findings(answer)))
        self.assertFalse(
            any(
                "Sydney Airport SYD to City of Sydney" in excerpt.sentence
                for excerpt in travel_rebuild.excerpts(trip, asked)
            )
        )

    def test_reverse_duration_is_not_this_trip(self) -> None:
        asked = "como llego a barcelona sants manana"
        place = "Calle del Humilladero, Barrio de los Austrias, Madrid, Spain"
        snippet = "El trayecto en tren de Barcelona a Madrid dura 3h 6min."
        trip = self.turn(asked, place, self.MADRID_URL, snippet)
        answer = (
            "El trayecto en tren de Barcelona a Madrid dura 3h 6min "
            f"([rail.example.test]({self.MADRID_URL}))."
        )
        self.assertNotIn("3h 6min", trip.kept(answer, trip.findings(answer)))
        self.assertFalse(
            any(
                "de Barcelona a Madrid" in excerpt.sentence
                for excerpt in travel_rebuild.excerpts(trip, asked)
            )
        )

    def test_reverse_departure_in_italian_is_not_this_trip(self) -> None:
        asked = "come arrivo a milano centrale domani mattina"
        place = "Via dei Fiori, Torino, Italy"
        snippet = "Il treno da Milano Centrale a Torino Porta Nuova parte alle 07:12."
        trip = self.turn(asked, place, self.TURIN_URL, snippet)
        answer = (
            "Il treno da Milano Centrale a Torino Porta Nuova parte alle 07:12 "
            f"([rail.example.test]({self.TURIN_URL}))."
        )
        self.assertNotIn("07:12", trip.kept(answer, trip.findings(answer)))
        self.assertFalse(
            any(
                "da Milano Centrale a Torino Porta Nuova" in excerpt.sentence
                for excerpt in travel_rebuild.excerpts(trip, asked)
            )
        )

    def test_reverse_frequency_and_platform_in_french_are_not_this_trip(self) -> None:
        asked = "comment je vais a marseille saint charles demain"
        place = "Rue des Lilas, Lyon, France"
        snippet = (
            "Le train de Marseille Saint-Charles à Lyon part toutes les 20 "
            "minutes du quai 4."
        )
        trip = self.turn(asked, place, self.LYON_URL, snippet)
        answer = (
            "Le train de Marseille Saint-Charles à Lyon part toutes les 20 "
            "minutes du quai 4 "
            f"([rail.example.test]({self.LYON_URL}))."
        )
        self.assertNotIn(
            "toutes les 20 minutes", trip.kept(answer, trip.findings(answer))
        )
        self.assertNotIn("quai 4", trip.kept(answer, trip.findings(answer)))
        self.assertFalse(
            any(
                "de Marseille Saint-Charles à Lyon" in excerpt.sentence
                for excerpt in travel_rebuild.excerpts(trip, asked)
            )
        )


class AnotherPlaceTests(unittest.TestCase):
    def sydney(self) -> travel_claims.Turn:
        return turn(
            SYDNEY_ASKED,
            SYDNEY_PLACE,
            {"link": LINE, "title": "T8 Airport & South Line", "snippet": LEAD},
            {"link": OTHER_CITY, "title": "Airtrain timetable", "snippet": BRISBANE},
        )

    def test_a_page_about_another_city_is_found(self) -> None:
        trip = self.sydney()
        self.assertEqual(
            travel_rebuild.elsewhere(trip, SYDNEY_ASKED, ""),
            {travel_claims.page_key(OTHER_CITY)},
        )

    def test_it_supports_no_value_and_is_not_listed(self) -> None:
        trip = self.sydney()
        trip.excluded = travel_rebuild.elsewhere(trip, SYDNEY_ASKED, "")
        found = trip.findings("The airport train takes 20 minutes.")
        self.assertEqual([q.text for q in found.values], ["20 minutes"])
        self.assertIn(OTHER_CITY, travel_rebuild.unrelated(trip, SYDNEY_ASKED, ""))

    def test_a_claim_cited_only_to_it_goes(self) -> None:
        trip = self.sydney()
        trip.excluded = travel_rebuild.elsewhere(trip, SYDNEY_ASKED, "")
        text = (
            f"Take the T8 to the airport ([wiki.example.test]({LINE})). The station "
            f"access fee is included in the fare ([airtrain.example.test]({OTHER_CITY}))."
        )
        kept = redirects.claims_without(
            text, lambda url: travel_claims.page_key(url) in trip.excluded
        )
        self.assertEqual(
            kept, f"Take the T8 to the airport ([wiki.example.test]({LINE}))."
        )

    def test_a_stop_of_the_answer_ties_a_page_to_the_trip(self) -> None:
        asked = "je dois etre a l'aeroport de geneve demain a 9h"
        trip = turn(
            asked,
            "Rue des Tilleuls, Gratte-Ciel, 69100 Villeurbanne, France",
            {
                "link": SNCF,
                "title": "Train Lyon Genève",
                "snippet": "Trains de Lyon Part-Dieu à Genève Aéroport.",
            },
            {
                "link": TCL,
                "title": "Métro A",
                "snippet": "La ligne A dessert Perrache, Bellecour et Part-Dieu.",
            },
        )
        answer = "Prenez la ligne A jusqu'à Lyon Part-Dieu, puis le train."
        self.assertEqual(travel_rebuild.elsewhere(trip, asked, answer), set())

    def test_without_any_page_about_the_trip_nothing_is_set_apart(self) -> None:
        trip = turn(
            SYDNEY_ASKED,
            SYDNEY_PLACE,
            {"link": OTHER_CITY, "title": "Airtrain timetable", "snippet": BRISBANE},
        )
        self.assertEqual(travel_rebuild.elsewhere(trip, SYDNEY_ASKED, ""), set())


class UncitedAnswerTests(unittest.TestCase):
    def test_an_answer_linking_no_page_gives_the_pages_about_the_trip(self) -> None:
        # Staging -141: the pages are given with what they say of the times.
        chosen = [
            travel_rebuild.Excerpt(
                SNCF, "Trains de Lyon Part-Dieu à Genève à 06:38 et 07:38.", named=True
            )
        ]
        text = "Pour la suite du trajet, vérifiez les horaires de l'opérateur."
        pointer = travel_rebuild._uncited(text, chosen, "fr")
        self.assertIn(f"[rail.example.test]({SNCF})", pointer)
        self.assertIn("« Trains de Lyon Part-Dieu à Genève à 06:38 et 07:38 »", pointer)
        cited = f"Le train part de Lyon ([rail.example.test]({SNCF}))."
        self.assertEqual(travel_rebuild._uncited(cited, chosen, "fr"), "")


class ForwardDirectionTests(unittest.TestCase):
    """A leg out of the starting point, or with no direction, stays a fact."""

    PLACE = "Wattle Lane, Sydney NSW 2000, Australia"

    def test_a_leg_out_of_the_starting_point_sharing_its_city_name_stays(self) -> None:
        self.assertFalse(
            travel_claims.reverse_leg(
                "The T8 runs from Wattle Lane Sydney to Sydney Airport every 10 minutes.",
                self.PLACE,
            )
        )

    def test_a_leg_to_a_station_of_the_starting_point_city_stays(self) -> None:
        self.assertFalse(
            travel_claims.reverse_leg(
                "Walk from Wattle Lane to Central Sydney, then take the T8.",
                self.PLACE,
            )
        )

    def test_a_connection_with_no_direction_stays(self) -> None:
        self.assertFalse(
            travel_claims.reverse_leg(
                "The T8 connects Sydney Airport with City of Sydney in 13 minutes.",
                self.PLACE,
            )
        )

    def test_the_opposite_leg_in_german_goes(self) -> None:
        self.assertTrue(
            travel_claims.reverse_leg(
                "Der ICE von Berlin Hauptbahnhof nach München fährt um 06:02.",
                "Blumenweg, Neuhausen, 80636 München, Deutschland",
            )
        )


class SectionLabelTests(unittest.TestCase):
    """Staging -120: a Munich - Berlin rewrite cited "(KEPT)" in place of a page."""

    def test_section_names_used_as_citations_go(self) -> None:
        text = (
            "From the main station, take a direct ICE to Berlin (KEPT). "
            "Confirm the train on the operator's site (KEPT, PLAN). "
            "The ride takes 4 hours [2] and the first train is early [SOURCE 3]."
        )
        self.assertEqual(
            travel_rebuild.unlabelled(text),
            "From the main station, take a direct ICE to Berlin. "
            "Confirm the train on the operator's site. "
            "The ride takes 4 hours [2] and the first train is early [3].",
        )

    def test_links_and_ordinary_parentheses_stay(self) -> None:
        text = (
            "Take line 8 (platform 3) to the airport "
            "([rail.example.test](https://rail.example.test/airport)); "
            "see [1](https://rail.example.test/one) and the plan (KEPT in mind)."
        )
        self.assertEqual(travel_rebuild.unlabelled(text), text)


class CitationFormTests(unittest.TestCase):
    """Staging -120: Madrid cited "[fuente: url]"; New York listed one site twice."""

    def test_worded_citations_become_links(self) -> None:
        text = (
            "El tren dura 2 horas [fuente: https://rail.example.test/madrid]. "
            "Der Zug fährt stündlich (Quelle: https://bahn.example.test/plan)."
        )
        self.assertEqual(
            travel_rebuild.citations(text),
            "El tren dura 2 horas ([rail.example.test](https://rail.example.test/madrid)). "
            "Der Zug fährt stündlich ([bahn.example.test](https://bahn.example.test/plan)).",
        )

    def test_one_link_per_site_in_a_group(self) -> None:
        text = (
            "Trains leave from Penn Station ([guide.example.test](https://guide.example.test/a), "
            "[guide.example.test](https://guide.example.test/b), "
            "[rail.example.test](https://rail.example.test/c))."
        )
        self.assertEqual(
            travel_rebuild.citations(text),
            "Trains leave from Penn Station ([guide.example.test](https://guide.example.test/a), "
            "[rail.example.test](https://rail.example.test/c)).",
        )

    def test_a_markdown_link_is_left_alone(self) -> None:
        text = "See [rail.example.test](https://rail.example.test/c) (platform 2)."
        self.assertEqual(travel_rebuild.citations(text), text)


if __name__ == "__main__":
    unittest.main()
