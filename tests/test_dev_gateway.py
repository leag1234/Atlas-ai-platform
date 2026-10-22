"""M15 code gateway: frozen requests, budget before I/O and real recorded deltas."""

from pathlib import Path
import json
from typing import Any
import unittest
import os
import aiohttp
from unittest.mock import patch, MagicMock
from collections.abc import AsyncIterator

from packages.dev_request import Request
import dev_gateway as gateway


class DevGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        env = patch.dict(
            os.environ,
            {
                "SCW_GENERATIVE_BASE_URL": "https://provider.invalid/v1",
                "SCW_GENERATIVE_API_KEY": "test-provider-key",
            },
        )
        env.start()
        self.addCleanup(env.stop)

    async def test_reservation_snapshot_and_recorded_stream(self) -> None:
        request = Request.model_validate(
            {"messages": [{"role": "user", "content": "Use save_value."}]}
        )
        plan = gateway.prepare(request)
        request.messages[0].content = "changed after reservation"
        self.assertNotIn("changed", plan.body)
        self.assertLessEqual(plan.reserved, 50000)
        for name in ("auto", "required", "named"):
            raw = json.loads(
                (Path(__file__).parent / "fixtures/devapi-streams.json").read_text()
            )[name].encode()

            async def chunks(plan: gateway.Plan) -> AsyncIterator[bytes]:
                for offset in range(0, len(raw), 7):
                    yield raw[offset : offset + 7]

            with patch.object(gateway, "transport", chunks):
                frames = [frame async for frame in gateway.events(plan)]
            self.assertGreater(len(frames), 3)
            self.assertGreater(frames[-1]["cost_micro_eur"], 0)
            self.assertEqual(frames[-1]["usage"]["prompt_tokens"], 166)
            self.assertTrue(any(f.get("choices") for f in frames[:-1]))

    async def test_large_context_refusal_and_broken_stream(self) -> None:
        valid = Request.model_validate(
            {"messages": [{"role": "user", "content": "x" * 24576}], "max_tokens": 128}
        )
        plan = gateway.prepare(valid)
        self.assertGreaterEqual(len(plan.body), 24576)
        for content in ("x" * 40000, "界" * 100000):
            with self.assertRaises(ValueError):
                gateway.prepare(
                    Request.model_validate(
                        {"messages": [{"role": "user", "content": content}]}
                    )
                )
        for raw in (b"data: {}\n\n", b"data: [DONE]\n\n", b"x" * 2100000):

            async def chunks(plan: gateway.Plan) -> AsyncIterator[bytes]:
                yield raw

            with (
                patch.object(gateway, "transport", chunks),
                self.assertRaises(ValueError),
            ):
                _ = [frame async for frame in gateway.events(plan)]
        payload: dict[str, Any]
        for payload in (
            {"messages": []},
            {"messages": [{"role": "tool", "tool_call_id": "unknown", "content": "x"}]},
            {"messages": [{"role": "user", "content": "x"}], "max_tokens": True},
            {"messages": [{"role": "user", "content": "x"}], "tool_choice": "required"},
            {"messages": [{"role": "user", "content": "x"}], "model": "outside"},
        ):
            with self.assertRaises(ValueError):
                Request.model_validate(payload)

    async def test_transport_success_failure_and_disconnect(self) -> None:
        plan = gateway.prepare(
            Request.model_validate({"messages": [{"role": "user", "content": "x"}]})
        )
        for mode in (200, 503, "disconnect"):
            with patch.object(aiohttp, "ClientSession") as factory:
                factory.return_value.__aenter__.return_value = MagicMock()
                session = factory.return_value.__aenter__.return_value
                session.post.return_value.__aenter__.return_value = MagicMock()
                response = session.post.return_value.__aenter__.return_value
                response.status = mode

                async def chunks(size: int) -> AsyncIterator[bytes]:
                    yield b"one fragment"

                response.content.iter_chunked.side_effect = chunks
                if mode == "disconnect":
                    session.post.return_value.__aenter__.side_effect = (
                        aiohttp.ClientConnectionError()
                    )
                if mode == 200:
                    self.assertEqual(
                        [b async for b in gateway.transport(plan)], [b"one fragment"]
                    )
                else:
                    with self.assertRaises(RuntimeError):
                        _ = [b async for b in gateway.transport(plan)]
                self.assertEqual(session.post.call_count, 1)
                self.assertEqual(
                    session.post.call_args.kwargs["data"], plan.body.encode()
                )

    def test_correlated_tool_history_and_named_choice(self) -> None:
        tool = {
            "type": "function",
            "function": {"name": "save_value", "parameters": {"type": "object"}},
        }
        call = {
            "id": "call-a",
            "type": "function",
            "function": {"name": "save_value", "arguments": '{"value":7}'},
        }
        messages = [
            {"role": "assistant", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "call-a", "content": "saved"},
        ]
        request = Request.model_validate(
            {
                "messages": messages,
                "tools": [tool],
                "tool_choice": {"type": "function", "function": {"name": "save_value"}},
            }
        )
        self.assertEqual(request.messages[1].tool_call_id, "call-a")
        body = json.loads(gateway.prepare(request).body)
        self.assertEqual(body["tools"][0]["type"], "function")
        self.assertEqual(body["messages"][1]["tool_calls"][0]["type"], "function")
        with self.assertRaises(ValueError):
            Request.model_validate({"messages": messages[:1], "tools": [tool]})
        with self.assertRaises(ValueError):
            Request.model_validate({"messages": messages, "tools": [tool, tool]})
