"""Trip answers on staging -115 (2026-11-18, seven trips on a phone).

A trip's answer call ran to the turn's deadline and the person read "took too
long" although the pages read gave the trains; an answer opened with the
remainder of a sentence whose first clause went ("De eso arriesgas ...");
a table lost its header labels and kept a row without its train.
"""

import asyncio
from decimal import Decimal
import unittest
from unittest.mock import AsyncMock, patch

from packages.profiles import PROFILES
from services.orchestrator import chat_pipeline, deadline, travel_claims
from services.orchestrator.loop import (
    Limits,
    Message,
    Query,
    Reservation,
    Result,
    Turn,
    answer_share,
    run,
)
from test_search_limit_closing import ANSWER, QUESTION, Research, offered, search
from test_trip_failures import COMPLETE, PAGE, REQUEST, stopped, trip


def turn(asked: str, place: str, language: str, text: str) -> travel_claims.Turn:
    found = travel_claims.Turn(
        asked=[asked], place=place, language=language, zone="Europe/Paris"
    )
    found.activate()
    found.collect({"link": "https://rail.example.test/trip", "text": text})
    return found


class SlowWriter:
    """Searches once, then writes its answer for ``seconds``."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.timeouts: list[float] = []

    def estimate(self, messages: list[Message]) -> Reservation:
        return Reservation(10, Decimal("0.001"))

    async def complete(self, messages: list[Message], timeout: float) -> Turn:
        self.timeouts.append(timeout)
        if "web_search" in offered(messages) and not any(
            message.get("role") == "tool" for message in messages
        ):
            return Turn("", (search("s1"),))
        await asyncio.sleep(self.seconds)
        return Turn(ANSWER)


async def chat_turn(model: SlowWriter, wall_clock: float) -> Result:
    return await run(
        Query(question=QUESTION, lang="en"),
        model,
        Research(allowance=100),
        "",
        Limits(profile=PROFILES[0], cost=Decimal("0.05"), wall_clock=wall_clock),
        final_only=True,
    )


class AnswerTimeTests(unittest.IsolatedAsyncioTestCase):
    def test_the_share_keeps_the_recovery_time_at_most_a_quarter(self) -> None:
        self.assertEqual(answer_share(100.0, 20.0), 80.0)
        self.assertEqual(answer_share(40.0, 20.0), 30.0)
        self.assertIsNone(answer_share(100.0, 0.0))

    async def test_a_slow_answer_call_leaves_time_for_the_recovery(self) -> None:
        model = SlowWriter(30)
        with deadline.answer_recovery(lambda: 0.5):
            started = asyncio.get_running_loop().time()
            result = await chat_turn(model, 2.0)
            spent = asyncio.get_running_loop().time() - started
        self.assertEqual((result.state, result.reason), ("stopped", "answer_time"))
        # Cut before the turn's deadline, with the recovery time still there.
        self.assertLess(spent, 1.9)
        self.assertLess(model.timeouts[-1], 2.0 - 0.4)

    async def test_without_a_recovery_the_deadline_is_unchanged(self) -> None:
        model = SlowWriter(30)
        result = await chat_turn(model, 1.0)
        self.assertEqual((result.state, result.reason), ("stopped", "wall_clock"))

    async def test_a_fast_answer_is_not_cut(self) -> None:
        model = SlowWriter(0.01)
        with deadline.answer_recovery(lambda: 0.5):
            result = await chat_turn(model, 2.0)
        self.assertEqual((result.state, result.text), ("done", ANSWER))

    async def test_a_cut_trip_answer_is_written_from_the_pages(self) -> None:
        item = stopped()
        text = (
            "Un train part de Caen à 08:05 et arrive à Paris Saint-Lazare à 10:12 "
            f"([rail.example.test]({PAGE}))."
        )
        call = AsyncMock(return_value={"text": text, "cost_eur": "0.0004"})
        with patch(COMPLETE, call):
            async with deadline.request_deadline(5):
                recovered = await chat_pipeline._recover_trip(
                    REQUEST, item, trip(), "Caen Paris", "answer_time"
                )
        self.assertTrue(recovered)
        self.assertEqual((item.state, item.reponse), ("done", text))
        # The rewrite stays inside the turn's deadline.
        assert call.await_args is not None
        self.assertLessEqual(call.await_args.args[0]["timeout"], 4)


class OpeningTests(unittest.TestCase):
    SOURCE = (
        "AVE 03252: salida 5:50, llegada 8:20, duración 2 h 30 min. "
        "Valmont Norte 06:57 - Orlen Sants 09:59."
    )

    def test_a_remainder_without_facts_of_its_own_goes_with_its_clause(self) -> None:
        checked = turn(
            "mañana tengo que estar en orlen sants, que tren cojo desde aqui?",
            "Calle del Olmo, Valmont",
            "es",
            self.SOURCE,
        ).check(
            "El AVE Valmont–Orlen tarda unas 2 h 45 min a 3 h; de eso arriesgas "
            "llegar justo o tarde (rail.example.test). Un ejemplo es el AVE 03252, "
            "que sale a las 5:50 y llega a las 8:20.",
            "es",
        )
        body = checked.split("\n\n")[0]
        self.assertEqual(
            body, "Un ejemplo es el AVE 03252, que sale a las 5:50 y llega a las 8:20."
        )

    def test_a_remainder_with_its_own_facts_stays(self) -> None:
        checked = turn(
            "mañana tengo que estar en orlen sants, que tren cojo desde aqui?",
            "Calle del Olmo, Valmont",
            "es",
            self.SOURCE,
        ).check(
            "El AVE tarda unas 2 h 45 min; el AVE 03252 sale a las 5:50 y llega a "
            "las 8:20.",
            "es",
        )
        self.assertTrue(
            checked.startswith("El AVE 03252 sale a las 5:50 y llega a las 8:20."),
            checked,
        )

    def test_clauses_left_out_one_after_another_leave_no_remainder(self) -> None:
        checked = turn(
            "mañana tengo que estar en orlen sants, que tren cojo desde aqui?",
            "Calle del Olmo, Valmont",
            "es",
            self.SOURCE,
        ).check(
            "Sal hacia las 4:10; el AVE tarda unas 2 h 45 min; de eso arriesgas "
            "llegar tarde. El AVE 03252 sale a las 5:50.",
            "es",
        )
        self.assertTrue(checked.startswith("El AVE 03252 sale a las 5:50."), checked)


class TableTests(unittest.TestCase):
    SOURCE = (
        "Orlen to Valmont. Express 233 departs Orlen 07:00, arrives Valmont 09:12, "
        "2h 12m. Express 315 departs 07:09, arrives 09:21. Rapid departs 06:21, "
        "arrives 09:06."
    )
    ASKED = "need to get to valmont station tomorow before 10 am from here"

    def table(self, header: str, rows: str) -> str:
        return (
            "Trains that work:\n\n"
            + header
            + "\n|---|---|---|---|\n"
            + rows
            + "\nPick the one that suits you."
        )

    def test_header_labels_are_never_claims(self) -> None:
        found = turn(self.ASKED, "Orlen Station, Orlen", "en", self.SOURCE)
        answer = self.table(
            "| Train | Depart Orlen | Arrive Valmont (before 10:00) | Duration |",
            "| Express 233 | 07:00 | 09:12 | 2h 12m |\n"
            "| Express 315 | 07:09 | 09:21 | 2h 12m |",
        )
        self.assertEqual(found.check(answer, "en"), answer)

    def test_a_duration_between_sourced_times_is_kept(self) -> None:
        found = turn(self.ASKED, "Orlen Station, Orlen", "en", self.SOURCE)
        self.assertEqual(
            [q.kind for q in travel_claims.quantities("2h 45m")], ["duration"]
        )
        answer = self.table(
            "| Train | Departs | Arrives | Duration |",
            "| Rapid | 06:21 | 09:06 | 2h 45m |\n"
            "| Express 233 | 07:00 | 09:12 | 2h 12m |",
        )
        self.assertEqual(found.check(answer, "en"), answer)

    def test_a_row_that_loses_its_first_cell_goes_whole(self) -> None:
        found = turn(self.ASKED, "Orlen Station, Orlen", "en", self.SOURCE)
        answer = (
            "Departures:\n\n| Departure | Arrival | Train |\n|---|---|---|\n"
            "| 06:05 | 08:50 | Rapid |\n| 07:00 | 09:12 | Express 233 |\n\nGood trip."
        )
        body = found.check(answer, "en").split("\n\nNot verified")[0]
        self.assertNotIn("Rapid", body)
        self.assertNotIn("—", body)
        self.assertIn("| Departure | Arrival | Train |", body)
        self.assertIn("| 07:00 | 09:12 | Express 233 |", body)


if __name__ == "__main__":
    unittest.main()
