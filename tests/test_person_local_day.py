"""The person's own day, and the model's words around a planned trip (staging -133).

UI trip battery of 2026-11-20 (00:15-00:25 UTC), each browser in its city's
time zone:

- New York, 19:25 on Thursday 19 November local, "tmrw by 11am": the journey
  planner, in the device's zone, planned Friday 2026-11-20, but the model was
  told "today" in the instance's default zone (Paris, already the 20th) and
  wrote "The journey planner returned times for Friday 2026-11-20, not your
  travel day of Saturday 2026-11-21." Open WebUI forwards no zone of its own:
  every "today" of the turn read the request default.
- Munich and Sydney: under the block's time-zone note, the model's reworded
  copy "All times are local (...) from published timetables; ..." stayed.

The clock is patched everywhere; places and streets are fictional.
"""

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from services.orchestrator import relative_dates, travel_claims, work_clock
from services.orchestrator import transit_planner as tp
from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.location import Location, Position
from services.orchestrator.location_memory import ConversationPositions
from services.orchestrator.loop import Reservation, Turn
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.stream_client import sink_context
from test_planned_trip_block import Planned, checked, planned

# 00:25 UTC on Friday 20 November 2026: 19:25 on Thursday 19 in New York,
# 01:25 on Friday 20 in Paris.
LATE = datetime(2026, 11, 20, 0, 25, tzinfo=timezone.utc)
NEW_YORK = (40.7506, -73.9935)
NYC_QUESTION = "hey i gotta be at boston south station tmrw by 11am, whats the best train from here?"
# The trip's day written out where the recorded plan needs it.
NYC_DATED = "hey i gotta be at boston south station on 2026-11-20 by 11am, whats the best train from here?"
MUNICH_QUESTION = "I need to be at Berlin Hauptbahnhof on 2026-11-20 by 2pm, how do I get there from here by train?"
SYDNEY_QUESTION = "how do i get to sydney airport from here on 2026-11-20, flight at 8am, public transport pls"


def chat(**fields: Any) -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role="user", content=NYC_QUESTION)], **fields
    )


def located(*, timezone: str | None = None, position: bool = True) -> Location:
    return Location(
        consent="once",
        position=Position(latitude=NEW_YORK[0], longitude=NEW_YORK[1])
        if position
        else None,
        timezone=timezone,
    )


class PersonZoneTest(unittest.TestCase):
    def test_the_interfaces_zone_comes_first(self) -> None:
        request = chat(ui_timezone="Asia/Tokyo", location=located())
        self.assertEqual(work_clock.settle_zone(request), "Asia/Tokyo")
        self.assertEqual(request.ui_timezone, "Asia/Tokyo")

    def test_without_it_the_zone_sent_with_the_location(self) -> None:
        request = chat(location=located(timezone="America/Chicago", position=False))
        self.assertEqual(work_clock.settle_zone(request), "America/Chicago")
        self.assertEqual(request.ui_timezone, "America/Chicago")

    def test_else_the_zone_where_the_device_is(self) -> None:
        # The browser's zone said UTC; the device is in New York.
        request = chat(location=located(timezone="UTC"))
        self.assertEqual(work_clock.settle_zone(request), "America/New_York")

    def test_a_position_shared_earlier_in_the_conversation(self) -> None:
        memory = ConversationPositions()
        request = chat()
        request.__dict__.update(
            daily_user="traveller@example.test", native_chat_id="c1"
        )
        with patch("services.orchestrator.location_memory.positions", memory):
            from services.orchestrator.location_memory import conversation_key

            memory.remember(conversation_key(request), located())
            self.assertEqual(work_clock.settle_zone(request), "America/New_York")

    def test_nothing_known_keeps_the_instance_default(self) -> None:
        request = chat()
        self.assertEqual(work_clock.settle_zone(request), "UTC")
        self.assertEqual(relative_dates.home_zone(request.ui_timezone), "Europe/Paris")
        # An object without a location or a conversation (an API caller).
        bare = SimpleNamespace(ui_timezone="UTC")
        self.assertEqual(work_clock.settle_zone(bare), "UTC")

    def test_a_work_turn_reads_the_same_zone(self) -> None:
        request = SimpleNamespace(ui_timezone="UTC", location=located())
        self.assertEqual(work_clock.request_zone(request), "America/New_York")
        moment = work_clock.current_moment(work_clock.request_zone(request), LATE)
        self.assertEqual(
            (moment["weekday"], moment["date"]), ("Thursday", "2026-11-19")
        )

    def test_the_new_york_turn_has_one_tomorrow(self) -> None:
        request = chat(location=located())
        zone = work_clock.settle_zone(request)
        context = relative_dates.calendar(zone, "en", LATE)
        self.assertIn("- today: Thursday 2026-11-19", context)
        self.assertIn("- tomorrow: Friday 2026-11-20", context)
        self.assertEqual(
            relative_dates.local_today(zone, LATE).isoformat(), "2026-11-19"
        )
        self.assertEqual(
            travel_claims.user_dates([NYC_QUESTION], zone, LATE), ["2026-11-20"]
        )
        # What the model was told before: the default zone's tomorrow.
        before = relative_dates.calendar("UTC", "en", LATE)
        self.assertIn("- tomorrow: Saturday 2026-11-21", before)

    def test_the_interface_sends_the_browsers_zone(self) -> None:
        script = Path("services/orchestrator/chat-ui.js").read_text()
        wrapper = script[script.index("window.fetch = async") :]
        wrapper = wrapper[: wrapper.index("lastChatSend")]
        self.assertIn("body.atlas_ui = true;", wrapper)
        self.assertIn("Intl.DateTimeFormat().resolvedOptions().timeZone", wrapper)
        self.assertIn("body.ui_timezone = zone", wrapper)


class TurnZoneTest(unittest.IsolatedAsyncioTestCase):
    """A chat turn's calendar is the person's: the turn replayed, model scripted."""

    async def test_the_calendar_is_in_the_zone_sent_with_the_location(self) -> None:
        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=400,
            ),
        )
        seen: list[str] = []

        async def complete(messages: object, timeout: float) -> Turn:
            seen.append(str(messages))
            return Turn("Fine.", usage=Reservation(10, Decimal(0)))

        async def sink(event: dict[str, object]) -> None:
            return None

        # A refused position still carries the browser's zone.
        request = ChatRequest(
            messages=[
                ChatMessage(
                    role="user", content="what's the weather tomorrow in Exampleton"
                )
            ],
            location=Location(consent="refused", timezone="America/New_York"),
        )
        token = sink_context.set(sink)
        try:
            with (
                patch("services.orchestrator.datecalc.utc_now", return_value=LATE),
                patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
                patch.object(model, "complete", side_effect=complete),
            ):
                await process(request, Interaction())
        finally:
            sink_context.reset(token)
        sent = "\n".join(seen)
        self.assertIn("tomorrow: Friday 2026-11-20", sent)
        self.assertNotIn("- tomorrow: Saturday 2026-11-21", sent)
        self.assertEqual(request.ui_timezone, "America/New_York")


class TripTextTest(Planned):
    def trip(
        self,
        plan: str,
        destination: str,
        deadline: str,
        question: str,
        zone: str,
        at: tuple[float, float],
    ) -> tuple[travel_claims.Turn, dict[str, Any]]:
        turn = travel_claims.Turn(
            active=True,
            language="en",
            zone=zone,
            asked=[question],
            position=Position(latitude=at[0], longitude=at[1]),
        )
        data = planned(
            plan,
            {
                "destination": destination,
                "from_device_position": True,
                "arrive_by": deadline,
            },
            turn,
        )
        self.assertEqual(data["outcome"], "planned")
        return turn, data

    def new_york(self) -> tuple[travel_claims.Turn, dict[str, Any]]:
        return self.trip(
            "new-york",
            "Boston South Station",
            "2026-11-20T11:00",
            NYC_DATED,
            "America/New_York",
            NEW_YORK,
        )

    def munich(self) -> tuple[travel_claims.Turn, dict[str, Any]]:
        return self.trip(
            "munich",
            "Berlin Hauptbahnhof",
            "2026-11-20T14:00",
            MUNICH_QUESTION,
            "Europe/Berlin",
            (48.15, 11.54),
        )

    def sydney(self) -> tuple[travel_claims.Turn, dict[str, Any]]:
        return self.trip(
            "sydney-stop",
            "Sydney Airport",
            "2026-11-20T07:00",
            SYDNEY_QUESTION,
            "Australia/Sydney",
            (-33.869, 151.21),
        )


class NoteCopyTest(TripTextTest):
    def test_the_reworded_time_zone_note_goes_munich_and_sydney(self) -> None:
        for trip, zone, question in (
            (self.munich, "Europe/Berlin", MUNICH_QUESTION),
            (self.sydney, "Australia/Sydney", SYDNEY_QUESTION),
        ):
            turn, data = trip()
            copy = (
                f"All times are local ({zone}) from published timetables; check "
                "them with the operator before you travel."
            )
            final, _ = checked(turn, data["rendered"] + "\n\n" + copy, "en", question)
            self.assertEqual(final, data["rendered"], final)

    def test_small_rewordings_in_each_language_go(self) -> None:
        turn, data = self.munich()
        for language, copy in (
            (
                "en",
                "Times shown are local (Berlin time), from the published timetables; check them with the operator before you travel.",
            ),
            (
                "fr",
                "Toutes les heures locales (Europe/Berlin), d'après les horaires publiés ; vérifiez-les auprès de l'exploitant avant de partir.",
            ),
            (
                "de",
                "Alle Ortszeiten (Europe/Berlin) laut veröffentlichtem Fahrplan; vor der Fahrt bitte beim Verkehrsunternehmen prüfen.",
            ),
            (
                "es",
                "Todas las horas locales (Europe/Berlin), según los horarios publicados; compruébelas con el operador antes de salir.",
            ),
            (
                "it",
                "Tutti gli orari sono locali (Europe/Berlin), secondo gli orari pubblicati; verificateli con l'operatore prima di partire.",
            ),
        ):
            note = tp.WORDS[language]["zones"].format(z="Europe/Berlin")
            notes = [set(tp._words(note))]
            self.assertTrue(tp._note_copy(copy, notes), (language, copy))
            # The note itself, and a sentence adding a clause of its own.
            self.assertTrue(tp._note_copy(note, notes))
            extended = (
                note.rstrip(".")
                + ", especially on weekends when engineering works close lines."
            )
            self.assertFalse(tp._note_copy(extended, notes), language)
        del turn, data

    def test_a_sentence_with_real_content_stays(self) -> None:
        turn, data = self.munich()
        advice = (
            "Times are local; the ICE 1100 has a bistro car, and a seat "
            "reservation is worth it on a Friday."
        )
        final, _ = checked(
            turn, data["rendered"] + "\n\n" + advice, "en", MUNICH_QUESTION
        )
        self.assertEqual(final, data["rendered"] + "\n\n" + advice)

    def test_only_the_copy_leaves_a_line_with_more(self) -> None:
        turn, data = self.munich()
        line = (
            "All times are local (Europe/Berlin) from published timetables; check "
            "them with the operator before you travel. Option 2 has the fewest changes."
        )
        final = tp.planned_answer("Here are your trains.\n\n" + line, turn)
        self.assertTrue(final.endswith("\n\nOption 2 has the fewest changes."), final)
        self.assertEqual(final.count("Times are local"), 1)
        self.assertNotIn("All times are local", final)


class PlannedDayTest(TripTextTest):
    DISPUTE = (
        "Note: The journey planner returned times for Friday 2026-11-20, not your "
        "travel day of Saturday 2026-11-21. Confirm Saturday’s exact times "
        "with the rail operator before you travel."
    )

    def test_a_sentence_disputing_the_planned_day_goes(self) -> None:
        turn, data = self.new_york()
        final, _ = checked(
            turn, data["rendered"] + "\n\n" + self.DISPUTE, "en", NYC_DATED
        )
        self.assertEqual(final, data["rendered"], final)

    def test_what_follows_it_without_the_other_day_stays(self) -> None:
        turn, data = self.new_york()
        line = self.DISPUTE + " The coach is the cheapest option."
        final = tp.planned_answer("Here are your options.\n\n" + line, turn)
        self.assertTrue(final.endswith("\n\nThe coach is the cheapest option."), final)
        self.assertNotIn("2026-11-21", final)
        self.assertNotIn("Saturday", final)

    def test_the_planned_day_alone_stays(self) -> None:
        turn, data = self.new_york()
        opening = "Tomorrow is Friday 2026-11-20; these trips arrive before 11:00:"
        final, _ = checked(turn, opening + "\n\n" + data["rendered"], "en", NYC_DATED)
        self.assertEqual(final, opening + "\n\n" + data["rendered"])

    def test_a_sentence_citing_a_page_read_stays(self) -> None:
        turn, _ = self.new_york()
        cited = (
            "Service on 2026-11-20 differs from 2026-11-21 "
            "([example-rail.test](https://example-rail.test/holiday))."
        )
        final = tp.planned_answer("Here are your options.\n\n" + cited, turn)
        self.assertIn(cited, final)


class Good133Test(TripTextTest):
    """The -133 answers that were right stay as they were (fixture times)."""

    def test_caen_opening_and_block(self) -> None:
        turn, data = self.caen()
        opening = (
            "Voici les horaires possibles demain, vendredi 20 novembre 2026, pour "
            "arriver à Gare de Lyon avant 12:30, depuis chez toi (12 rue Fictive, Caen) :"
        )
        answer = opening + "\n\n" + data["rendered"]
        self.assertEqual(tp.planned_answer(answer, turn), answer)
        rest, first = tp.without_plans(answer, turn)
        self.assertEqual(tp.planned_answer(rest, turn, first=first), answer)

    def test_madrid_opening_and_block(self) -> None:
        turn, data = self.madrid()
        opening = (
            "El viernes 20 de noviembre de 2026, desde Calle Ficticia 9 (Madrid), "
            "estas son las opciones:"
        )
        answer = opening + "\n\n" + data["rendered"]
        rest, first = tp.without_plans(answer, turn)
        self.assertEqual(tp.planned_answer(rest, turn, first=first), answer)

    def test_tokyo_and_sydney_blocks_alone(self) -> None:
        tokyo = (
            "need to get to kyoto station on 2026-11-20 before 10 am by "
            "shinkansen from here"
        )
        for turn, data in (
            self.sydney(),
            self.trip(
                "tokyo",
                "Kyoto Station",
                "2026-11-20T10:00",
                tokyo,
                "Asia/Tokyo",
                (35.681, 139.767),
            ),
        ):
            rest, first = tp.without_plans(data["rendered"], turn)
            self.assertEqual(
                tp.planned_answer(rest, turn, first=first), data["rendered"]
            )
