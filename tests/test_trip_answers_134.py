"""Trip answers of staging -134 (UI trip battery, 2026-11-20 01:55-02:05 UTC).

Each browser was in its city's time zone and shared its position:

- New York: "Two FlixBus coach options also get you there earlier: walk 4
  minutes to New York Midtown ..." was left out as unverified although
  options 1 and 3 of the computed plan are exactly these coaches. The count
  opening the sentence ("Two") was read into the name "Two FlixBus", a word
  the plan does not write.
- New York: "Check the times with the operator before you travel." stayed
  under the block's own note saying the same, a copy much shorter than it.
- Munich and Tokyo: the person asked "from here" with the device position
  shared, but the model gave the planner a place name of its own
  ("Moosach / Neuhausen-Nymphenburg (Munich)", "Tokyo Station (Chiyoda)"),
  then wrote that the walk from the starting point was not covered.

The clock is patched everywhere; streets are fictional.
"""

from typing import Any

from services.orchestrator import route_facts, travel_claims
from services.orchestrator import transit_planner as tp
from services.orchestrator.location import Position, own_position_origin
from test_person_local_day import MUNICH_QUESTION, NYC_DATED, TripTextTest
from test_planned_trip_block import checked, planned
from test_transit_planner import Planner

TOKYO_QUESTION = (
    "need to get to kyoto station on 2026-11-20 before 10 am by shinkansen "
    "from here, which trains can i take"
)
MUNICH = (48.15, 11.54)


class Trips134(TripTextTest):
    def tokyo(self) -> tuple[travel_claims.Turn, dict[str, Any]]:
        return self.trip(
            "tokyo",
            "Kyoto Station",
            "2026-11-20T10:00",
            TOKYO_QUESTION,
            "Asia/Tokyo",
            (35.681, 139.767),
        )


class PlanSummaryTest(Trips134):
    """A summary of the plan's options keeps its count and comparisons."""

    def test_the_two_flixbus_options_stay(self) -> None:
        turn, data = self.new_york()
        for summary in (
            "Two FlixBus coach options also get you there earlier: walk 5 minutes "
            "to New York Midtown (31st St & 8th Ave) and take the coach to Boston "
            "(South Station).",
            "Both FlixBus coaches leave from New York Midtown (31st St & 8th Ave) "
            "and take you to Boston (South Station).",
            "Two FlixBus coach options also get you there earlier: walk 5 minutes "
            "to New York Midtown (31st St & 8th Ave).",
        ):
            answer = data["rendered"] + "\n\n" + summary
            final, _ = checked(turn, answer, "en", NYC_DATED)
            self.assertEqual(final, answer, summary)

    def test_a_count_the_plan_does_not_give_is_left_out(self) -> None:
        turn, data = self.new_york()
        summary = (
            "Three FlixBus coach options also get you there earlier: walk 5 "
            "minutes to New York Midtown (31st St & 8th Ave) and take the coach "
            "to Boston (South Station)."
        )
        final, _ = checked(turn, data["rendered"] + "\n\n" + summary, "en", NYC_DATED)
        self.assertNotIn("Three FlixBus coach options also", final.split("“")[0])
        self.assertIn("Not verified", final)

    def test_counts_are_read_against_the_options(self) -> None:
        turn, data = self.new_york()
        planned_words = route_facts.Planned(turn.plan, route_facts.Sources([], [], []))
        self.assertEqual(len(planned_words.options), 3)
        self.assertTrue(planned_words.counted(["two", "flixbus"]))
        self.assertTrue(planned_words.counted(["three", "options"]))
        self.assertFalse(planned_words.counted(["three", "flixbus"]))
        self.assertFalse(planned_words.counted(["four", "options"]))
        self.assertFalse(planned_words.counted(["two"]))
        self.assertFalse(planned_words.counted(["two", "three", "flixbus"]))


class ShortNoteCopyTest(Trips134):
    def test_the_short_copy_goes_new_york(self) -> None:
        turn, data = self.new_york()
        answer = (
            data["rendered"]
            + "\n\nCheck the times with the operator before you travel."
        )
        final, _ = checked(turn, answer, "en", NYC_DATED)
        self.assertEqual(final, data["rendered"], final)

    def test_short_copies_in_each_language(self) -> None:
        for language, copy in (
            ("en", "Check the times with the operator before you travel."),
            ("fr", "Vérifiez les horaires auprès de l'exploitant avant de partir."),
            ("de", "Vor der Fahrt beim Verkehrsunternehmen prüfen."),
            ("es", "Compruebe los horarios con el operador antes de salir."),
            ("it", "Verificate con l'operatore prima di partire."),
        ):
            note = tp.WORDS[language]["zones"].format(z="Europe/Berlin")
            notes = [set(tp._words(note))]
            self.assertTrue(tp._note_copy(copy, notes), (language, copy))

    def test_a_short_sentence_with_its_own_content_stays(self) -> None:
        note = tp.WORDS["en"]["zones"].format(z="America/New_York")
        notes = [set(tp._words(note))]
        for sentence in (
            "Check the coach's luggage rules with the operator before you travel.",
            "Check with the operator before you travel from Penn Station.",
            "Times are local.",
            "Book the coach early: fares rise close to departure.",
        ):
            self.assertFalse(tp._note_copy(sentence, notes), sentence)
        turn, data = self.new_york()
        advice = "Check the coach's luggage rules with the operator before you travel."
        final, _ = checked(turn, data["rendered"] + "\n\n" + advice, "en", NYC_DATED)
        self.assertEqual(final, data["rendered"] + "\n\n" + advice)


class OwnPositionTest(Trips134):
    """ "From here" with the device position shared plans from that position."""

    def test_the_phrases_of_the_persons_own_position(self) -> None:
        for text in (
            MUNICH_QUESTION,
            TOKYO_QUESTION,
            "whats the best train from here?",
            "mañana tengo que estar en barcelona sants a las 13h, que tren cojo desde aqui?",
            "comment aller à la gare de Lyon d'ici ?",
            "je pars depuis ici demain matin",
            "wie komme ich von hier zum Hauptbahnhof?",
            "come arrivo a Milano Centrale da qui?",
            "how do I get to the airport from my current location",
            "itinéraire depuis ma position jusqu'à Lyon",
        ):
            self.assertTrue(own_position_origin(text), text)
        for text in (
            "je dois être à Paris d'ici demain",
            "d'ici la fin de la semaine, je vais à Lyon",
            "devo essere a Milano da qui a domani",
            "from here on, I only take trains",
            "how do I get to Boston from home",
            "comment aller à Lyon depuis chez moi",
            "how do I get from Lindenweg 4, Munich to here",
        ):
            self.assertFalse(own_position_origin(text), text)

    def munich_turn(self, question: str = MUNICH_QUESTION) -> travel_claims.Turn:
        return travel_claims.Turn(
            active=True,
            language="en",
            zone="Europe/Berlin",
            asked=[question],
            position=Position(latitude=MUNICH[0], longitude=MUNICH[1]),
        )

    def test_a_place_the_model_wrote_gives_way_to_the_position(self) -> None:
        turn = self.munich_turn()
        data = planned(
            "munich",
            {
                "destination": "Berlin Hauptbahnhof",
                # Not in the fake geocoder: geocoding it would find nothing.
                "origin": "Moosach / Neuhausen-Nymphenburg (Munich)",
                "arrive_by": "2026-11-20T14:00",
            },
            turn,
        )
        self.assertEqual(data["outcome"], "planned")
        self.assertEqual(data["origin"], "your position")
        self.assertIn(
            "Walk 5 min from your position to Rotkreuzplatz", data["rendered"]
        )
        self.assertTrue(turn.plan_from_device)
        self.assertEqual(turn.plan_starts, ["Rotkreuzplatz"])

    def test_the_request_is_rewritten_only_for_the_own_position(self) -> None:
        request = tp.TransitPlan(
            destination="Berlin Hauptbahnhof", origin="Tokyo Station (Chiyoda)"
        )
        moved = tp.from_own_position(request, ["earlier", TOKYO_QUESTION])
        self.assertTrue(moved.from_device_position)
        self.assertIsNone(moved.origin)
        # The person's last message decides, not an earlier one.
        self.assertIs(
            tp.from_own_position(request, [TOKYO_QUESTION, "and from Tokyo Station?"]),
            request,
        )
        # An origin the model wrote as "here" is the position too.
        here = tp.TransitPlan(destination="Kyoto Station", origin="my current location")
        self.assertTrue(
            tp.from_own_position(here, [TOKYO_QUESTION]).from_device_position
        )

    def test_an_origin_the_person_named_stays(self) -> None:
        question = (
            "from here, I mean Lindenweg 4, Munich: how do I get to Berlin "
            "Hauptbahnhof on 2026-11-20 by 2pm?"
        )
        planner = Planner("munich")
        turn = self.munich_turn(question)
        data = planned(
            "munich",
            {
                "destination": "Berlin Hauptbahnhof",
                "origin": "Lindenweg 4, Munich",
                "arrive_by": "2026-11-20T14:00",
            },
            turn,
        )
        self.assertNotEqual(data["origin"], "your position")
        self.assertFalse(turn.plan_from_device)
        del planner

    def test_without_the_words_the_models_origin_stays(self) -> None:
        turn = self.munich_turn(
            "I need to be at Berlin Hauptbahnhof on 2026-11-20 by 2pm, I leave "
            "from Lindenweg 4, Munich"
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
        self.assertNotEqual(data["origin"], "your position")

    def test_without_a_device_position_nothing_changes(self) -> None:
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
        self.assertNotEqual(data["origin"], "your position")


class StartCaveatTest(Trips134):
    """A caveat that the first walk is not covered goes when the plan has it."""

    def test_the_munich_and_tokyo_caveats_go(self) -> None:
        turn, data = self.munich()
        caveat = (
            "The walk from your starting point to Rotkreuzplatz is not covered by "
            "the sources—verify that leg in the operator’s journey planner."
        )
        final, _ = checked(
            turn, data["rendered"] + "\n\n" + caveat, "en", MUNICH_QUESTION
        )
        self.assertEqual(final, data["rendered"], final)
        turn, data = self.tokyo()
        turn.place, turn.place_names = "1 Fictive Lane, Chiyoda", ["Chiyoda"]
        caveat = (
            "The route from your starting point (1 Fictive Lane, Chiyoda) to Tokyo "
            "Station is not covered by the sources; check it in the operator’s "
            "journey planner."
        )
        final, _ = checked(
            turn, data["rendered"] + "\n\n" + caveat, "en", TOKYO_QUESTION
        )
        self.assertEqual(final, data["rendered"], final)

    def test_in_other_languages(self) -> None:
        turn, data = self.munich()
        covered = tp._start_covered(turn)
        assert covered is not None
        for sentence in (
            "Le trajet à pied depuis votre position jusqu'à Rotkreuzplatz n'est pas "
            "couvert par les sources.",
            "Der Fußweg von Ihrem Standort zum Rotkreuzplatz ist in den Quellen "
            "nicht angegeben.",
            "El trayecto desde tu posición hasta Rotkreuzplatz no figura en las "
            "fuentes.",
        ):
            self.assertTrue(covered(sentence), sentence)

    def test_other_caveats_stay(self) -> None:
        turn, data = self.munich()
        covered = tp._start_covered(turn)
        assert covered is not None
        for sentence in (
            # Another leg, a stop the first legs do not reach.
            "The platform at Hauptbahnhof Nord is not given by the sources.",
            # A first stop without the start: no gap about the first walk.
            "Lifts at Rotkreuzplatz are not listed in the sources.",
            # A time of its own says more than the plan.
            "From your position, the 07:04 metro at Rotkreuzplatz is not confirmed "
            "by the sources.",
            # Advice, no gap.
            "From your starting point, Rotkreuzplatz is the closest metro stop.",
        ):
            self.assertFalse(covered(sentence), sentence)

    def test_a_plan_from_another_origin_keeps_the_caveat(self) -> None:
        turn, data = self.munich()
        turn.plan_from_device = False
        turn.plan_origin = "Lindenweg 4 (Munich)"
        covered = tp._start_covered(turn)
        assert covered is not None
        self.assertFalse(
            covered(
                "The walk from your starting point to Rotkreuzplatz is not covered "
                "by the sources."
            )
        )
        self.assertTrue(
            covered(
                "The walk from Lindenweg 4 to Rotkreuzplatz is not covered by the sources."
            )
        )


class Good134Test(Trips134):
    """The -134 answers that were right stay as they were (fixture times)."""

    def test_caen_madrid_sydney_new_york(self) -> None:
        turn, data = self.caen()
        opening = (
            "Voici les horaires possibles demain, vendredi 20 novembre 2026, pour "
            "arriver à Gare de Lyon (Paris) avant 12:30, depuis votre position à "
            "Caen (12 rue Fictive) :"
        )
        answer = opening + "\n\n" + data["rendered"]
        rest, first = tp.without_plans(answer, turn)
        self.assertEqual(tp.planned_answer(rest, turn, first=first), answer)
        turn, data = self.madrid()
        opening = (
            "Mañana es viernes, 20 de noviembre de 2026. Para estar en "
            "Barcelona-Sants a las 13:00, estos son los trenes que te valen desde "
            "Calle Ficticia 9 (Madrid):"
        )
        answer = opening + "\n\n" + data["rendered"]
        rest, first = tp.without_plans(answer, turn)
        self.assertEqual(tp.planned_answer(rest, turn, first=first), answer)
        turn, data = self.sydney()
        advice = (
            "Option 1 gives the most buffer before your flight. No source covers "
            "whether track work might affect these services, so check Sydney "
            "Trains service alerts before leaving."
        )
        answer = data["rendered"] + "\n\n" + advice
        rest, first = tp.without_plans(answer, turn)
        self.assertEqual(tp.planned_answer(rest, turn, first=first), answer)
        turn, data = self.new_york()
        rest, first = tp.without_plans(data["rendered"], turn)
        self.assertEqual(tp.planned_answer(rest, turn, first=first), data["rendered"])
