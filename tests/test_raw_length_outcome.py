"""Direct models: an output cut by max_tokens is a length outcome, never a provider failure.

Pilot 2026-11-17: reasoning spent the whole max_tokens, the gateway retried the same
request twice and the caller received 502 provider_error after about two minutes.
"""

from collections.abc import AsyncGenerator
import json
from typing import Any
import unittest
from unittest.mock import patch

import dev_gateway as gateway
from devapi_support import Environment

BODY: dict[str, Any] = {
    "model": "atlas-code",
    "messages": [
        {"role": "user", "content": "Prove a fictional lemma, then list ten cases."}
    ],
    "max_tokens": 64,
}
SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}


def recording(delta: dict[str, Any], reason: str, completion_tokens: int = 64) -> bytes:
    frames = [
        {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
        {
            "choices": [],
            "usage": {"prompt_tokens": 50, "completion_tokens": completion_tokens},
        },
    ]
    return (
        "".join("data: " + json.dumps(f) + "\n\n" for f in frames) + "data: [DONE]\n\n"
    ).encode()


REASONING_CUT = recording({"reasoning_content": "fictional reasoning"}, "length")
REASONING_STOP = recording({"reasoning_content": "fictional reasoning"}, "stop", 8)
ANSWER = recording({"content": "Done."}, "stop", 2)


class RawLengthOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Environment()
        self.addCleanup(self.env.close)
        self.plans: list[gateway.Plan] = []

    def replay(self, *streams: bytes) -> None:
        async def transport(plan: gateway.Plan) -> AsyncGenerator[bytes, None]:
            index = len(self.plans)
            self.plans.append(plan)
            yield streams[min(index, len(streams) - 1)]

        patcher = patch.object(gateway, "transport", transport)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, payload: dict[str, Any]) -> Any:
        return self.env.client.post(
            "/v1/chat/completions", headers=self.env.headers, json=payload
        )

    def test_reasoning_that_exhausts_max_tokens_returns_length_once(self) -> None:
        self.replay(REASONING_CUT, ANSWER)
        response = self.post(BODY)
        self.assertEqual(response.status_code, 200, response.text)
        choice = response.json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "length")
        self.assertFalse(choice["message"]["content"])
        self.assertEqual(response.json()["usage"]["completion_tokens"], 64)
        self.assertEqual(len(self.plans), 1, "the same request was sent again")

    def test_stream_ends_with_length_and_no_error(self) -> None:
        self.replay(REASONING_CUT, ANSWER)
        response = self.post(BODY | {"stream": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"finish_reason": "length"', response.text)
        self.assertNotIn('"error"', response.text)
        self.assertIn("[DONE]", response.text)
        self.assertEqual(len(self.plans), 1)

    def test_cut_json_is_returned_as_written_with_length(self) -> None:
        self.replay(recording({"content": '{"value": '}, "length"), ANSWER)
        response = self.post(
            BODY
            | {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "value", "schema": SCHEMA, "strict": True},
                }
            }
        )
        self.assertEqual(response.status_code, 200, response.text)
        choice = response.json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "length")
        self.assertEqual(choice["message"]["content"], '{"value": ')
        self.assertEqual(len(self.plans), 1)

    def test_reasoning_only_stop_is_still_retried(self) -> None:
        self.replay(REASONING_STOP, ANSWER)
        response = self.post(BODY)
        self.assertEqual(response.status_code, 200, response.text)
        choice = response.json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(choice["message"]["content"], "Done.")
        self.assertEqual(len(self.plans), 2)


if __name__ == "__main__":
    unittest.main()
