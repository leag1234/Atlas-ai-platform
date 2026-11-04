"""The gateway's embedding call: order, encodings, validation and price, without network."""

import asyncio
import json
import os
from typing import Any
import unittest
from unittest.mock import patch

import embeddings
from serverless_support import environment


class FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status, self.body = status, body

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def json(self, content_type: object = None) -> Any:
        return self.body


class FakeSession:
    sent: list[dict[str, Any]] = []
    reply: tuple[int, Any] = (200, {})

    def __init__(self, **options: object) -> None:
        pass

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def post(self, url: str, data: str, **options: object) -> FakeResponse:
        FakeSession.sent.append({"url": url, **json.loads(data)})
        return FakeResponse(*FakeSession.reply)


def run(texts: list[str], **options: Any) -> dict[str, Any]:
    with (
        patch.dict(os.environ, environment()),
        patch("embeddings.aiohttp.ClientSession", FakeSession),
    ):
        return asyncio.run(
            embeddings.embed(
                texts,
                dimensions=options.get("dimensions"),
                encoding=options.get("encoding", "float"),
                timeout=5,
            )
        )


class GatewayEmbeddingsTests(unittest.TestCase):
    def test_rows_are_returned_in_input_order(self) -> None:
        FakeSession.reply = (
            200,
            {
                "data": [
                    {"index": 1, "embedding": [0.2, 0.3]},
                    {"index": 0, "embedding": [0.0, 0.1]},
                ],
                "usage": {"prompt_tokens": 9},
            },
        )
        result = run(["a", "b"], dimensions=2)
        self.assertEqual(result["vectors"], [[0.0, 0.1], [0.2, 0.3]])
        self.assertEqual(result["tokens"], 9)
        self.assertEqual(result["cost_micro"], 1)
        self.assertEqual(FakeSession.sent[-1]["dimensions"], 2)
        self.assertTrue(FakeSession.sent[-1]["url"].endswith("/embeddings"))

    def test_base64_and_invalid_answers(self) -> None:
        FakeSession.reply = (
            200,
            {
                "data": [{"index": 0, "embedding": "AAAA"}],
                "usage": {"prompt_tokens": 2},
            },
        )
        self.assertEqual(run(["a"], encoding="base64")["vectors"], ["AAAA"])
        for reply in (
            (500, {}),
            (200, {"data": [], "usage": {"prompt_tokens": 1}}),
            (
                200,
                {
                    "data": [{"index": 3, "embedding": [1.0]}],
                    "usage": {"prompt_tokens": 1},
                },
            ),
            (
                200,
                {
                    "data": [{"index": 0, "embedding": "x"}],
                    "usage": {"prompt_tokens": 1},
                },
            ),
            (200, {"data": [{"index": 0, "embedding": [1.0]}]}),
        ):
            FakeSession.reply = reply
            with self.assertRaises(RuntimeError):
                run(["a"])

    def test_estimate_covers_the_measured_cost(self) -> None:
        with patch.dict(os.environ, environment()):
            self.assertGreaterEqual(embeddings.estimated_micro(["x" * 4000]), 1)
            self.assertGreaterEqual(
                embeddings.estimated_micro(["x" * 400000]),
                embeddings.price_micro(100000),
            )


if __name__ == "__main__":
    unittest.main()
