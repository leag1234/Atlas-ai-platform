"""Preserve verbatim context but identify the active public request distinctly."""

from decimal import Decimal
import unittest
import json
from services.orchestrator.loop import Limits, Message, Query, Reservation, Turn, run


class CurrentQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_profiles_preserve_history_and_mark_only_latest_request(self) -> None:
        for profile in ("atlas-qwen", "atlas-glm"):
            history: list[Message] = [
                {"role": "user", "content": "What was the old rate?"}
            ]
            seen: list[Message] = []

            class Model:
                def estimate(self, messages: list[Message]) -> Reservation:
                    return Reservation(1, Decimal(0))

                async def complete(
                    self, messages: list[Message], timeout: float
                ) -> Turn:
                    seen.extend(messages)
                    return Turn("The current limit follows from the cycle count.")

            class Tools:
                def estimate(self, call: object) -> Reservation:
                    raise AssertionError("No research needed")

                async def execute(self, call: object, timeout: float) -> Message:
                    raise AssertionError("No research needed")

            query = "Et maintenant, quel plafond théorique avec une sortie infinie ?"
            await run(
                Query(question=query, lang="fr"),
                Model(),
                Tools(),
                "Expert.",
                Limits(profile=profile),
                history=history,
            )
            self.assertEqual(seen[1], history[0])
            current = str(seen[-1]["content"])
            self.assertIn(query, current)
            self.assertEqual(json.loads(current)["question"], query)
            self.assertEqual(json.loads(current)["lang"], "fr")
            self.assertIn("CURRENT REQUEST", current)
            self.assertNotIn("old rate", current)
            self.assertIn("earlier turns are background", current)
            self.assertEqual(
                history, [{"role": "user", "content": "What was the old rate?"}]
            )
