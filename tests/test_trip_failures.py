"""A trip answer that failed is recovered from the pages read, or said as it is.

Staging -102 (2026-11-17, Caen-Paris on a phone): the answer model returned
nothing, its recovery attempt wrote its source record instead of an answer
(9453 characters, cut at the output cap), and the person read "an unexpected
error" although the pages read gave the trains. The pages now give the answer;
when nothing was read, the failure is the model service's, said as such (D5).
"""

import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from packages.user_errors import ui_failure, unexpected_failure
from services.orchestrator import chat_pipeline, travel_claims
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.interactions import Interaction

PAGE = "https://rail.example.test/caen-paris"
COMPLETE = "services.orchestrator.unsourced_details._complete"


def trip() -> travel_claims.Turn:
    turn = travel_claims.Turn(
        asked=["Demain déjeuner gare de Lyon à midi trente, depuis chez moi"]
    )
    turn.located(place="Rue des Glycines, 14000 Caen, France")
    turn.retrieved = lambda: [PAGE]
    turn.collect(
        {
            "link": PAGE,
            "snippet": "Train Caen 08:05 → Paris Saint-Lazare 10:12, durée 2 h 07 min.",
        }
    )
    return turn


REQUEST = ChatRequest.model_validate(
    {"messages": [{"role": "user", "content": "Caen Paris demain"}]}
)


def stopped(cost: float = 0.0) -> Interaction:
    item = Interaction()
    item.task_type, item.state, item.cout_eur = "research", "stopped", cost
    return item


class RecoverTripTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_trip_answer_is_written_from_the_pages(self) -> None:
        item = stopped(0.01)
        text = (
            "Un train part de Caen à 08:05 et arrive à Paris Saint-Lazare à 10:12 "
            f"([rail.example.test]({PAGE}))."
        )
        call = AsyncMock(return_value={"text": text, "cost_eur": "0.0004"})
        with patch(COMPLETE, call):
            recovered = await chat_pipeline._recover_trip(
                REQUEST, item, trip(), "Caen Paris", "provider_error"
            )
        self.assertTrue(recovered)
        self.assertEqual((item.state, item.reponse), ("done", text))
        self.assertEqual(item.erreurs, ["provider_error"])
        self.assertGreater(item.cout_eur, 0.01)

    async def test_refusals_are_never_recovered(self) -> None:
        for reason in ("cost_budget", "provider_quota", "timeout"):
            item = stopped()
            call = AsyncMock()
            with self.subTest(reason=reason), patch(COMPLETE, call):
                self.assertFalse(
                    await chat_pipeline._recover_trip(
                        REQUEST, item, trip(), "Caen Paris", reason
                    )
                )
                call.assert_not_awaited()

    async def test_a_turn_that_is_no_trip_is_left_alone(self) -> None:
        item = stopped()
        self.assertFalse(
            await chat_pipeline._recover_trip(
                REQUEST,
                item,
                travel_claims.Turn(),
                "Bonjour",
                "provider_error",
            )
        )


class StoppedAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_model_answer_is_not_an_unexpected_error(self) -> None:
        async def process(payload: ChatRequest, item: Interaction) -> None:
            item.state = "stopped"
            item.erreurs.append("provider_error")
            item.reponse = "Explicit stop: provider_error."

        payload = ChatRequest.model_validate(
            {
                "atlas_ui": True,
                "ui_locale": "fr",
                "messages": [
                    {"role": "user", "content": "Un train Caen Paris demain ?"}
                ],
            }
        )
        item = Interaction()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
        ):
            text = "".join(
                [
                    str(chunk)
                    async for chunk in response(
                        payload, item, time.monotonic(), process
                    ).body_iterator
                ]
            )
        readable = ui_failure("provider_error", "fr")
        assert readable is not None
        self.assertIn(json.dumps(readable)[1:-1], text)
        self.assertNotIn(json.dumps(unexpected_failure("fr"))[1:-1], text)
        self.assertEqual(item.erreurs[-1], "provider_error")


if __name__ == "__main__":
    unittest.main()
