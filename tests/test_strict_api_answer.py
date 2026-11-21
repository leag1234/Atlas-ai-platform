"""A strict assistant turn that is not streamed answers like a streamed one.

Staging -141 and -142 (2026-11-21): every non-streamed strict API turn failed
fast as 502 request_stopped. The owned server answered, but the plain owned
completion carried no cost, and the orchestrator refuses a profile answer
without its measured cost (invalid_profile_cost, swallowed as provider_error).
"""

from collections.abc import AsyncIterator
from decimal import Decimal
import os
import unittest
from unittest.mock import patch

from agent_provider import AgentProvider
from packages import privacy
from serverless_support import environment
from services.orchestrator.model import Configuration, GatewayModel

ANSWER = "The capital of the fictional Republic of Arvel is Duncastle."


async def owned_stream(*args: object) -> AsyncIterator[dict[str, object]]:
    """The owned call as the gateway streams it: text, then the decoder result."""
    yield {"delta": {"content": ANSWER}}
    yield {
        "result": {
            "text": ANSWER,
            "calls": [],
            "usage": {"prompt_tokens": 3970, "completion_tokens": 11},
            "finish_reason": "stop",
        }
    }


class StrictAnswerWithoutStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_profile_turn_not_streamed_is_answered(self) -> None:
        sent: list[dict[str, object]] = []

        async def gateway(
            url: str, payload: dict[str, object], timeout: float
        ) -> dict[str, object]:
            self.assertTrue(url.endswith("/agent/complete"))
            sent.append(payload)
            return await AgentProvider().complete(payload)

        model = GatewayModel(
            "http://gateway",
            Configuration(
                input_eur_per_mtok=Decimal("0.6"),
                output_eur_per_mtok=Decimal("3.6"),
                max_tokens=2048,
            ),
        )
        model.configure_quality("atlas-qwen", 6000)
        model.tools = []
        token = privacy.elevate("strict")
        try:
            with (
                patch.dict(
                    os.environ,
                    environment()
                    | {"ATLAS_OWNED_VLLM_BASE_URL": "https://owned.example.invalid/v1"},
                ),
                patch("stream_transport.retry_stream", owned_stream),
                patch.object(GatewayModel, "post", staticmethod(gateway)),
            ):
                turn = await model.complete(
                    [{"role": "user", "content": "What is the capital of Arvel?"}],
                    60,
                )
        finally:
            privacy.reset(token)
        self.assertEqual(sent[0]["privacy"], "strict")
        self.assertEqual(turn.text, ANSWER)
        self.assertEqual(model.spent, Decimal("0"))
        self.assertEqual(model.provider_model, "owned-vllm-model")


if __name__ == "__main__":
    unittest.main()
