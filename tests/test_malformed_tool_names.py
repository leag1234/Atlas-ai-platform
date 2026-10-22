"""Task A / M26 D5: invalid upstream names are not request-size failures."""

from collections.abc import AsyncIterator
from decimal import Decimal
import json
import os
import unittest
from unittest.mock import patch

from agent_provider import AgentProvider
from packages.limits import ProviderFailure
from quality import input_bound, stream_quality
from serverless import ServerlessPolicy
from serverless_support import environment
from stream_transport import attempt
from test_quality_gateway import request
from test_stream_transport import Content, Session
from test_streaming_decoder import end, frame


class Pieces(Content):
    def __init__(self, pieces: list[bytes]) -> None:
        self.pieces = pieces

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        for piece in self.pieces:
            yield piece


def response(name: str, *, streaming: bool, fragmented: bool = False) -> Session:
    session = Session()
    call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }
    if streaming:
        names = (
            [name[i : i + 40] for i in range(0, len(name), 40)]
            if fragmented
            else [name]
        )
        pieces = [frame({"content": "Provisional draft."})]
        for i, part in enumerate(names):
            pieces.append(
                frame(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1" if i == 0 else "",
                                "function": {
                                    "name": part,
                                    "arguments": "{}" if i == 0 else "",
                                },
                            }
                        ]
                    }
                )
            )
        pieces.append(end("tool_calls"))
    else:
        pieces = [
            json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": "Provisional draft.",
                                "tool_calls": [call],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 8},
                }
            ).encode()
        ]
    session.response.content = Pieces(pieces)
    return session


def answer_session() -> Session:
    session = Session()
    session.response.content = Pieces(
        [frame({"content": "The corrected result is 42."}), end()]
    )
    return session


class MalformedToolNameTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_and_json_names_are_provider_failures(self) -> None:
        for length in (92, 128, 277):
            for mode in ("json", "sse", "fragmented"):
                name = "x" * length
                session = response(
                    name, streaming=mode != "json", fragmented=mode == "fragmented"
                )
                with (
                    self.subTest(length=length, mode=mode),
                    patch.dict(os.environ, environment()),
                    patch("aiohttp.ClientSession", return_value=session),
                    self.assertRaises(ProviderFailure) as caught,
                ):
                    model = ServerlessPolicy().models["code"]
                    if mode == "json":
                        await AgentProvider()._complete(
                            request(), "https://provider.example", model, "", 5.0
                        )
                    else:
                        _ = [e async for e in attempt(request(), model, 5.0)]
                self.assertEqual(caught.exception.code, "malformed_tool_call")
                self.assertIn(
                    "provider returned a malformed tool call",
                    str(caught.exception).lower(),
                )
                self.assertNotIn(name, str(caught.exception))
                self.assertNotIn("Content Too Large", str(caught.exception))

    async def test_drop_invalid_turn_and_recover_once_without_tools(self) -> None:
        for length in (92, 128, 277):
            for streaming in (False, True):
                bad = response("x" * length, streaming=True)
                good = answer_session()
                req = request(
                    tools=[{"type": "function", "function": {"name": "calculator"}}]
                )
                with (
                    self.subTest(length=length, streaming=streaming),
                    patch.dict(os.environ, environment()),
                    patch("aiohttp.ClientSession", side_effect=[bad, good]) as client,
                ):
                    if streaming:
                        events = [e async for e in stream_quality(AgentProvider(), req)]
                        answer = events[-1]["result"]
                        self.assertNotIn("Provisional draft", str(events))
                    else:
                        answer = await AgentProvider().complete(req.model_dump())
                    self.assertEqual(client.call_count, 2)
                    assert isinstance(answer, dict)
                    self.assertEqual(answer["text"], "The corrected result is 42.")
                    self.assertEqual(answer["calls"], [])
                    self.assertEqual(answer["provider_retries"], 1)
                    assert isinstance(good.body, dict)
                    self.assertNotIn("tools", good.body)
                    self.assertNotIn("x" * length, json.dumps(good.body))
                    self.assertNotIn("Provisional draft", json.dumps(good.body))
                    policy = ServerlessPolicy()
                    assert isinstance(bad.body, dict)
                    expected = policy.cost(
                        str(bad.body["model"]),
                        input_bound(req.messages, req.tools),
                        int(str(bad.body["max_tokens"])),
                    ) + policy.cost(str(good.body["model"]), 20, 8)
                    self.assertEqual(Decimal(str(answer["cost_eur"])), expected)
                    self.assertLessEqual(expected, Decimal("0.10"))

    async def test_repeated_malformed_names_stop_with_true_cause(self) -> None:
        for length in (92, 128, 277):
            with (
                self.subTest(length=length),
                patch.dict(os.environ, environment()),
                patch(
                    "aiohttp.ClientSession",
                    side_effect=[
                        response("x" * length, streaming=True),
                        response("x" * length, streaming=True),
                    ],
                ) as client,
                self.assertRaises(ProviderFailure) as caught,
            ):
                await AgentProvider().complete(
                    request(
                        tools=[{"type": "function", "function": {"name": "calculator"}}]
                    ).model_dump()
                )
            self.assertEqual(client.call_count, 2)
            self.assertEqual(caught.exception.code, "malformed_tool_call")
            self.assertEqual(caught.exception.retries, 1)
            self.assertNotIn("x" * length, caught.exception.detail)

    async def test_json_fallback_recovers_without_the_invalid_call(self) -> None:
        bad = response("x" * 277, streaming=False)
        good = Session()
        good.response.content = Pieces(
            [
                json.dumps(
                    {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "content": "The corrected result is 42.",
                                    "tool_calls": [],
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 8},
                    }
                ).encode()
            ]
        )
        with (
            patch.dict(os.environ, environment()),
            patch("aiohttp.ClientSession", side_effect=[bad, good]) as client,
        ):
            answer = await AgentProvider().complete(request(profile=None).model_dump())
        self.assertEqual(client.call_count, 2)
        self.assertEqual(answer["text"], "The corrected result is 42.")
        assert isinstance(good.body, dict)
        self.assertEqual(good.body["tools"], [])
        self.assertNotIn("x" * 277, json.dumps(good.body))

    async def test_valid_80_character_name_still_reaches_the_caller(self) -> None:
        for streaming in (False, True):
            with (
                self.subTest(streaming=streaming),
                patch.dict(os.environ, environment()),
                patch(
                    "aiohttp.ClientSession",
                    return_value=response("x" * 80, streaming=streaming),
                ),
            ):
                model = ServerlessPolicy().models["code"]
                if streaming:
                    events = [e async for e in attempt(request(), model, 5.0)]
                    answer = events[-1]["result"]
                else:
                    answer = await AgentProvider()._complete(
                        request(), "https://provider.example", model, "", 5.0
                    )
                assert isinstance(answer, dict)
                self.assertIn("x" * 80, str(answer["calls"]))

    async def test_http_failure_names_malformed_provider_output(self) -> None:
        import asyncio
        import threading
        from unittest.mock import Mock
        import aiohttp
        from gateway_cpu import CPUModels
        from http_gateway import serve
        from packages.limits import MalformedToolCall

        async def failure(
            instance: AgentProvider, payload: object
        ) -> dict[str, object]:
            raise MalformedToolCall(277, 1)

        with (
            patch.object(AgentProvider, "complete", failure),
            patch.dict(os.environ, {"ESCALATION_MODEL": "local"}),
        ):
            gateway = serve(Mock(spec=CPUModels), 0)
            thread = threading.Thread(target=gateway.serve_forever, daemon=True)
            thread.start()
            try:
                async with aiohttp.ClientSession() as client:
                    async with client.post(
                        f"http://127.0.0.1:{gateway.server_port}/agent/complete",
                        json=request().model_dump(),
                    ) as reply:
                        body = await reply.json()
                        self.assertEqual(reply.status, 502)
                        self.assertEqual(body["code"], "provider_failure")
                        self.assertIn(
                            "provider returned a malformed tool call", body["message"]
                        )
                        self.assertIn("277", body["message"])
                        self.assertIn("80", body["message"])
                        self.assertNotIn("Content Too Large", body["message"])
            finally:
                await asyncio.to_thread(gateway.shutdown)
                gateway.server_close()
                await asyncio.to_thread(thread.join, 2)

    async def test_failed_recovery_keeps_cause_but_budget_refusal_is_distinct(
        self,
    ) -> None:
        from collections.abc import AsyncGenerator
        from agent_provider import AgentRequest
        from packages.limits import MalformedToolCall, ProviderLimitError

        for failure in (
            TimeoutError(),
            RuntimeError("provider_error"),
            ProviderLimitError("cost_budget", 100001, 100000, "microEUR"),
        ):
            calls = 0

            async def upstream(
                req: AgentRequest, model: str, timeout: float
            ) -> AsyncGenerator[dict[str, object], None]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise MalformedToolCall(128)
                raise failure
                yield {}

            with (
                self.subTest(failure=type(failure).__name__),
                patch.dict(os.environ, environment()),
                patch("stream_transport.attempt", upstream),
            ):
                if isinstance(failure, ProviderLimitError):
                    with self.assertRaises(ProviderLimitError) as budget:
                        await AgentProvider().complete(request().model_dump())
                    self.assertIs(budget.exception, failure)
                else:
                    with self.assertRaises(ProviderFailure) as provider:
                        await AgentProvider().complete(request().model_dump())
                    self.assertEqual(provider.exception.code, "malformed_tool_call")
                    self.assertEqual(provider.exception.retries, 1)
            self.assertEqual(calls, 2)
