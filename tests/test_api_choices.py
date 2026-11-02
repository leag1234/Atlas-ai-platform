"""Several choices (n) and log probabilities on Chat Completions for the direct models."""

from collections.abc import AsyncGenerator
import json
import os
import unittest
from typing import Any
from unittest.mock import patch

import dev_gateway
from services.orchestrator.dev_chat import Output, RawChatInput

KEYS = {"MISTRAL_API_KEY": "test-only", "SCW_GENERATIVE_API_KEY": "test-only"}


def request(**options: Any) -> RawChatInput:
    return RawChatInput.model_validate(
        {
            "model": "raw/public-routing",
            "messages": [{"role": "user", "content": "Name a colour."}],
            "max_tokens": 20,
            **options,
        }
    )


def token(text: str) -> dict[str, Any]:
    return {
        "token": text,
        "logprob": -0.1,
        "bytes": list(text.encode()),
        "top_logprobs": [],
    }


def sse(frames: list[dict[str, Any]]) -> bytes:
    return (
        b"".join(b"data: " + json.dumps(f).encode() + b"\n\n" for f in frames)
        + b"data: [DONE]\n\n"
    )


def provider_stream(logprobs: bool) -> list[dict[str, Any]]:
    def choice(
        index: int, text: str | None, reason: str | None = None
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "index": index,
            "delta": {"content": text} if text else {},
            "finish_reason": reason,
        }
        if logprobs and text:
            entry["logprobs"] = {"content": [token(text)]}
        return entry

    return [
        {"choices": [choice(0, "Red")]},
        {"choices": [choice(1, "Blue")]},
        {"choices": [choice(1, "", "stop")]},
        {"choices": [choice(0, "", "stop")]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 4}},
    ]


class ChoicesTests(unittest.IsolatedAsyncioTestCase):
    async def run_gateway(
        self, req: RawChatInput, frames: list[dict[str, Any]]
    ) -> tuple[Output, list[dict[str, Any]], dict[str, Any]]:
        async def transport(plan: dev_gateway.Plan) -> AsyncGenerator[bytes, None]:
            yield sse(frames)

        with patch.dict(os.environ, KEYS):
            plan = dev_gateway.prepare_api(req, "atlas-code", "key-test")
        output = Output("test", req)
        chunks = []
        with patch.object(dev_gateway, "transport", transport):
            async for event in dev_gateway.attempt_events(plan):
                chunks.append(output.feed(event))
        output.usage = chunks[-1].get("usage") or {
            "prompt_tokens": 10,
            "completion_tokens": 4,
        }
        return output, chunks, json.loads(plan.body)

    async def test_two_choices_with_log_probabilities(self) -> None:
        output, chunks, body = await self.run_gateway(
            request(n=2, logprobs=True, top_logprobs=2), provider_stream(True)
        )
        self.assertEqual(
            (body["n"], body["logprobs"], body["top_logprobs"]), (2, True, 2)
        )
        result = output.result()
        self.assertEqual([c["index"] for c in result["choices"]], [0, 1])
        self.assertEqual(
            [c["message"]["content"] for c in result["choices"]], ["Red", "Blue"]
        )
        self.assertEqual(
            [c["finish_reason"] for c in result["choices"]], ["stop", "stop"]
        )
        self.assertEqual(
            result["choices"][1]["logprobs"]["content"][0]["token"], "Blue"
        )
        streamed = [c["index"] for chunk in chunks for c in chunk["choices"]]
        self.assertEqual(streamed, [0, 1, 1, 0])

    async def test_budget_counts_every_choice(self) -> None:
        with patch.dict(os.environ, KEYS):
            one = dev_gateway.prepare_api(request(), "atlas-code", "key-test")
            two = dev_gateway.prepare_api(request(n=2), "atlas-code", "key-test")
        self.assertGreater(two.reserved, one.reserved)

    async def test_default_request_is_unchanged(self) -> None:
        with patch.dict(os.environ, KEYS):
            plan = dev_gateway.prepare_api(request(), "atlas-code", "key-test")
        body = json.loads(plan.body)
        self.assertNotIn("n", body)
        self.assertNotIn("logprobs", body)

    async def test_a_missing_choice_is_an_incomplete_response(self) -> None:
        frames = [
            f
            for f in provider_stream(False)
            if not any(c["index"] == 1 for c in f["choices"])
        ]
        with self.assertRaises(ValueError):
            await self.run_gateway(request(n=2), frames)

    async def test_choice_outside_the_request_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            await self.run_gateway(request(), provider_stream(False))


class AnswerTokensTests(unittest.TestCase):
    def test_reasoning_and_control_tokens_are_not_answer_tokens(self) -> None:
        output = Output("test", request(logprobs=True))
        frames = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": ""},
                        "finish_reason": None,
                        "logprobs": {"content": [token("Simple"), token(".")]},
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Blue"},
                        "finish_reason": None,
                        "logprobs": {"content": [token("</think>"), token("Blue")]},
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                        "logprobs": {"content": [token("<|user|>")]},
                    }
                ]
            },
        ]
        for frame in frames:
            output.feed(frame)
        output.usage = {"prompt_tokens": 10, "completion_tokens": 6}
        tokens = output.result()["choices"][0]["logprobs"]["content"]
        self.assertEqual([t["token"] for t in tokens], ["Blue"])
