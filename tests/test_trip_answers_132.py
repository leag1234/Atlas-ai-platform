"""Trip answers of staging -132 (UI trip battery, 2026-11-19).

The plan blocks were right everywhere; around them:

- Caen to Paris and Tokyo to Kyoto: the model's summary of the computed plan
  ("soit seulement 13 minutes avant votre déjeuner" for 12:17 and 12:30,
  "three direct Nozomi Shinkansen options ... to Kyoto Station") was listed
  as not verified. Arithmetic on the plan's own times, and lines, places and
  "direct" written with the plan's words or the person's, are the plan's.
- Orphans: Munich ended with "All options ... deadline:" once its list (a
  restatement of the plan) went; Tokyo opened with "From Tokyo Station
  (Chiyoda)", the cut start of a sentence; Caen gave the time-zone note twice.
- Villeurbanne to Geneva airport: under the doubtful plan the model's own
  "Il n'existe pas de liaison directe" (no page read) stayed. The search for
  the direct services named the start's stop ("Gratte-Ciel Rollet") and read
  two encyclopedia articles about it.
- Sydney: the browser was on UTC, the device in Sydney, where it was already
  07:55 on the 20th: "tomorrow" was planned for the 20th and no itinerary
  could arrive in time, yet the planner's attribution was printed. The note
  quoted a claim cut after "direct".

Addresses are fictional; times are those of the recorded plans.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

from services.orchestrator import travel_claims
from services.orchestrator import transit_planner as tp
from services.orchestrator.location import Position
from test_planned_trip_block import Planned, checked, planned
from test_transit_planner import Planner

CAEN_QUESTION = (
    "Le 2026-11-20 j'ai un dejeuner a Gare de Lyon a midi trente, donne moi "
    "les horaires possibles le matin pour etre a l'heure, depuis chez moi"
)
# The trip's day written out: the tests never depend on today's date.
TOKYO_QUESTION = (
    "need to get to kyoto station on 2026-11-20 before 10 am by shinkansen "
    "from here, which trains can i take"
)
MUNICH_QUESTION = (
    "I need to be at Berlin Hauptbahnhof on 2026-11-20 by 2pm, how do I get "
    "there from here by train?"
)
GENEVA_QUESTION = (
    "je dois etre a l'aeroport de geneve le 2026-11-20 a 9h, comment j'y vais "
    "depuis chez moi en transport en commun ?"
)
SYDNEY_QUESTION = (
    "how do i get to sydney airport from here tomorow morning, flight at 8am, "
    "public transport pls"
)


class PlanSummaryTest(Planned):
    def tokyo(self) -> tuple[travel_claims.Turn, dict[str, Any]]:
        turn = travel_claims.Turn(
            active=True,
            language="en",
            zone="Asia/Tokyo",
            asked=[TOKYO_QUESTION],
            position=Position(latitude=35.681, longitude=139.767),
        )
        data = planned(
            "tokyo",
            {
                "destination": "Kyoto Station",
                "from_device_position": True,
                "arrive_by": "2026-11-20T10:00",
            },
            turn,
        )
        return turn, data

    def caen_turn(self) -> tuple[travel_claims.Turn, dict[str, Any]]:
        turn = travel_claims.Turn(
            active=True,
            language="fr",
            zone="Europe/Paris",
            asked=[CAEN_QUESTION],
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

    def test_a_margin_before_the_deadline_is_the_plans_arithmetic(self) -> None:
        turn, data = self.caen_turn()
        opening = (
            "Voici les horaires possibles pour arriver à la Gare de Lyon à 12:30 :"
        )
        advice = (
            "La dernière option ne vous laisse que 13 minutes avant votre "
            "déjeuner : la deuxième est plus sûre."
        )
        answer = opening + "\n\n" + data["rendered"] + "\n\n" + advice
        final, call = checked(turn, answer, "fr", CAEN_QUESTION)
        self.assertEqual(final, answer)
        call.assert_not_awaited()
        # The -132 sentence also gives a time of the plan: it restates the
        # plan and goes, without any note.
        restated = (
            "L'option 3 arrive à 12:17, soit seulement 13 minutes avant votre déjeuner."
        )
        final, _ = checked(
            turn,
            opening + "\n\n" + data["rendered"] + "\n\n" + restated,
            "fr",
            CAEN_QUESTION,
        )
        self.assertEqual(final, opening + "\n\n" + data["rendered"])

    def test_a_duration_the_plan_does_not_give_is_still_left_out(self) -> None:
        turn, _ = self.caen_turn()
        found = turn.findings("Le TER met 47 minutes de Caen à Paris.")
        self.assertEqual([claim.text for claim in found.values], ["47 minutes"])

    def test_the_models_copy_of_the_time_zone_note_goes(self) -> None:
        turn, data = self.caen_turn()
        copy = (
            "Heures locales (Europe/Paris), d'après les horaires publiés; "
            "vérifiez-les auprès de l'exploitant avant de partir."
        )
        opening = "Voici les horaires possibles demain :"
        answer = opening + "\n\n" + data["rendered"] + "\n\n" + copy
        final, _ = checked(turn, answer, "fr", CAEN_QUESTION)
        self.assertEqual(final.count("Heures locales (Europe/Paris)"), 1, final)
        self.assertEqual(final, opening + "\n\n" + data["rendered"])

    def test_the_summary_of_the_plan_is_no_unverified_route(self) -> None:
        turn, data = self.tokyo()
        self.assertEqual(data["outcome"], "planned")
        opening = (
            "From Tokyo Station (Chiyoda), three direct Nozomi Shinkansen options "
            "on Friday 2026-11-20 get you to Kyoto Station before 10:00:"
        )
        answer = opening + "\n\n" + data["rendered"]
        final, call = checked(turn, answer, "en", TOKYO_QUESTION)
        self.assertEqual(final, answer)
        self.assertNotIn("Not verified", final)
        call.assert_not_awaited()

    def test_a_line_the_plan_never_names_is_still_checked(self) -> None:
        turn, _ = self.tokyo()
        found = turn.findings(
            "Take the Hikari 505 from Shinagawa to Kyoto Station, it is direct."
        )
        self.assertTrue(found.route.spans, found.labels())

    def test_a_restatement_announced_by_a_colon_leaves_no_colon(self) -> None:
        # Munich: the model gave a copy of the block with one leg reworded,
        # then a list restating its times; code gave the block back.
        turn = travel_claims.Turn(
            active=True, language="en", zone="Europe/Berlin", asked=[MUNICH_QUESTION]
        )
        data = planned(
            "munich",
            {
                "destination": "Berlin Hauptbahnhof",
                "origin": "Lindenweg 4, Munich",
                "arrive_by": "2026-11-20T14:00",
            },
            turn,
        )
        reworded = data["rendered"].replace("Walk ", "Stroll ", 1)
        self.assertNotEqual(reworded, data["rendered"])
        options = [
            line.split("**: ", 1)[1]
            for line in data["rendered"].splitlines()
            if line.startswith("**Option")
        ]
        answer = (
            reworded
            + "\n\nAll options are for Friday 2026-11-20 and arrive well before "
            "your 14:00 deadline:\n\n" + "\n".join(f"- {option}" for option in options)
        )
        final, _ = checked(turn, answer, "en", MUNICH_QUESTION)
        self.assertEqual(final, data["rendered"])

    def test_a_colon_line_before_the_block_still_introduces_it(self) -> None:
        turn, data = self.caen_turn()
        opening = "Voici les départs possibles demain :"
        final = tp.planned_answer(opening + "\n\nBon voyage.", turn)
        self.assertEqual(final, opening + "\n\n" + data["rendered"] + "\n\nBon voyage.")

    def test_the_cut_start_of_a_sentence_goes(self) -> None:
        turn, data = self.tokyo()
        before = (
            "From Tokyo Station (Chiyoda), three direct Nozomi Shinkansen options "
            "get you to Kyoto Station before 10:00:"
        )
        answer = "From Tokyo Station (Chiyoda)\n\n" + data["rendered"]
        self.assertEqual(tp.without_fragments(answer, before, turn), data["rendered"])
        # A whole sentence of the model's, or a line it wrote as such, stays.
        whole = "Take the first one if you can.\n\n" + data["rendered"]
        self.assertEqual(tp.without_fragments(whole, whole, turn), whole)
        # Without a plan block, nothing is touched.
        bare = travel_claims.Turn(active=True)
        self.assertEqual(tp.without_fragments(answer, before, bare), answer)


GENEVA_PROSE = (
    "Pour arriver à l'aéroport de Genève (GVA) vendredi 20 novembre 2026 à 9h, "
    "voici un itinéraire en transport en commun depuis Gratte-Ciel, "
    "Villeurbanne, calculé sur les horaires ouverts. Il n'existe pas de liaison "
    "directe ; le trajet passe par Lyon puis Genève.\n\n"
    "Les horaires proviennent des données ouvertes et sont peut-être "
    "incomplets ; vérifiez-les auprès des exploitants avant de partir."
)


class DoubtfulProseTest(Planned):
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

    def test_unread_claims_about_the_services_go(self) -> None:
        turn, data = self.geneva()
        answer = GENEVA_PROSE + "\n\n" + data["rendered"]
        final, _ = checked(turn, answer, "fr", GENEVA_QUESTION)
        self.assertTrue(final.startswith("Aucune liaison raisonnable"), final)
        self.assertNotIn("Il n'existe pas de liaison directe", final)
        self.assertNotIn("le trajet passe par Lyon", final)
        self.assertNotIn("Les horaires proviennent", final)
        self.assertTrue(final.rstrip().endswith(data["rendered"].rstrip()), final)

    def test_a_sentence_citing_a_page_read_stays_alone(self) -> None:
        turn, data = self.geneva()
        page = "https://www.example-rail.test/lyon-geneve"
        linked = (
            "Des TER directs relient Lyon Part-Dieu à Genève "
            f"([example-rail.test]({page}))."
        )
        answer = (
            "**Trains directs**\n\n"
            "Le trajet est simple. " + linked + " Prévoyez de la marge.\n\n"
            "**Autres pistes**\n\n" + data["rendered"]
        )
        final = tp.doubtful_answer(answer, "fr", turn)
        self.assertTrue(final.startswith("**Trains directs**\n\n" + linked), final)
        self.assertNotIn("Le trajet est simple", final)
        self.assertNotIn("Prévoyez", final)
        self.assertNotIn("Autres pistes", final)
        self.assertIn(data["rendered"], final)

    def test_the_search_names_a_stops_town_and_reads_pages_about_the_trip(
        self,
    ) -> None:
        stop = {
            "type": "STOP",
            "name": "Gratte-Ciel Rollet",
            "areas": [{"name": "Villeurbanne", "default": True}],
        }
        query = asyncio.run(
            tp._direct_query(
                tp.Client(tp.settings()),  # type: ignore[arg-type]
                (45.769, 4.882),
                stop,
                {"name": "Genève Aéroport"},
                "fr",
            )
        )
        self.assertTrue(query.startswith("Villeurbanne Genève Aéroport"), query)
        # The -132 results: articles about the stop's words first, the
        # airport's access page fourth.
        results = {
            "results": [
                {
                    "link": "https://encyclopedia.example.test/wiki/Gratte-ciel",
                    "title": "Gratte-ciel",
                    "snippet": "Un gratte-ciel est un immeuble très haut.",
                },
                {
                    "link": "https://encyclopedia.example.test/wiki/Rollet",
                    "title": "Rollet, cycliste",
                    "snippet": "Coureur cycliste né à Villeurbanne.",
                },
                {
                    "link": "https://airport.example.test/acceder/navette",
                    "title": "Navette aéroport",
                    "snippet": "Accéder à l'aéroport en navette.",
                },
                {
                    "link": "https://airport-gva.example.test/fr/acces-et-transports",
                    "title": "Accès et transports – Genève Aéroport",
                    "snippet": "Train, bus et car jusqu'au terminal.",
                },
            ]
        }
        self.assertEqual(
            tp.direct_candidates(results, destination="Genève Aéroport"),
            ["https://airport-gva.example.test/fr/acces-et-transports"],
        )
        self.assertEqual(tp.place_words("aéroport de Genève"), {"geneve"})


class SydneyTest(Planned):
    def test_tomorrow_is_the_day_after_the_devices_day(self) -> None:
        self.assertEqual(tp.zone_at(-33.8688, 151.2093), "Australia/Sydney")
        self.assertEqual(tp.zone_at(35.681, 139.767), "Asia/Tokyo")
        # The browser on UTC, the device in Sydney (staging -132).
        turn = travel_claims.Turn(
            active=True,
            language="en",
            zone="UTC",
            asked=[SYDNEY_QUESTION],
            position=Position(latitude=-33.869, longitude=151.21),
        )
        planner = Planner("sydney")

        async def fetch(_self: Any, path: str, params: dict[str, str]) -> Any:
            return await planner(path, params)

        token = travel_claims.current.set(turn)
        try:
            with patch.object(tp.Client, "_fetch", fetch):
                asyncio.run(
                    tp.execute(
                        json.dumps(
                            {
                                "destination": "Sydney Airport",
                                "from_device_position": True,
                                "arrive_by": "2026-11-20T07:00",
                            }
                        ),
                        30.0,
                    )
                )
        finally:
            travel_claims.current.reset(token)
        day = travel_claims.user_dates([SYDNEY_QUESTION], "Australia/Sydney")[0]
        times = [p["time"] for path, p in planner.calls if path.endswith("/plan")]
        self.assertTrue(times, planner.calls)
        self.assertTrue(times[0].startswith(day + "T07:00"), times)

    def test_no_attribution_without_a_plan_shown(self) -> None:
        turn = travel_claims.Turn(active=True, language="en", asked=[SYDNEY_QUESTION])
        turn.planned = True
        turn.compute(
            "No itinerary arrives by 2026-11-20 07:00.\n"
            + tp.attribution("en", tp.settings())  # type: ignore[arg-type]
        )
        answer = (
            "Take the T8 Airport line from the city "
            "([example-transport.test](https://example-transport.test/airport))."
        )
        final, _ = checked(turn, answer, "en", SYDNEY_QUESTION)
        self.assertNotIn("Transitous", final)
        self.assertNotIn("OpenStreetMap", final)

    def test_a_note_quotes_the_claim_to_the_end_of_its_clause(self) -> None:
        sentence = (
            "The Airport Link is an underground rail line that provides direct "
            "access to the terminals."
        )
        turn = travel_claims.Turn(active=True, language="en", asked=[SYDNEY_QUESTION])
        turn.collect(
            {
                "url": "https://example-transport.test/airport",
                "text": "Trains on the T8 Airport line stop at the International "
                "and Domestic stations.",
            }
        )
        labels = turn.findings(sentence).labels()
        self.assertEqual(len(labels), 1, labels)
        self.assertTrue(
            labels[0].endswith("access to the terminals”") or labels[0].endswith("…”"),
            labels,
        )
        self.assertNotIn("provides direct”", labels[0])
