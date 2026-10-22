"""Transport uses real recorded frames, yields before completion, closes on cancel."""

from collections.abc import AsyncIterator
from contextlib import aclosing
import os
import json
import unittest
from unittest.mock import patch

from agent_provider import AgentProvider
from serverless_support import environment
from serverless import ServerlessPolicy
from test_streaming_decoder import frame, end


class Content:
    closed = False

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        yield frame({"content": "Bonjour"})
        yield end()


class Response:
    status = 200
    content = Content()

    async def __aenter__(self) -> "Response":
        return self

    async def __aexit__(self, *args: object) -> None:
        self.content.closed = True


class Session:
    def __init__(self, **kwargs: object) -> None:
        self.response = Response()
        self.response.content = Content()
        self.body: object = None

    async def __aenter__(self) -> "Session":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def post(self, url: str, **kwargs: object) -> Response:
        self.body = kwargs["json"]
        return self.response


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_completion_diagnostic_has_no_payload(self) -> None:
        from agent_provider import AgentRequest
        from provider_retry import TransientProviderError
        from stream_transport import attempt

        class EmptyContent(Content):
            async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
                yield end()

        session = Session()
        session.response.content = EmptyContent()
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.aiohttp.ClientSession", return_value=session),
            self.assertLogs("stream_transport", level="WARNING") as logs,
        ):
            with self.assertRaises(TransientProviderError) as caught:
                _ = [
                    event
                    async for event in attempt(
                        AgentRequest(
                            messages=[{"role": "user", "content": "private-question"}],
                            tools=[],
                            profile="atlas-qwen",
                            timeout=5.0,
                        ),
                        ServerlessPolicy().models["text"],
                        5.0,
                    )
                ]
        self.assertIsNotNone(caught.exception.usage)
        assert caught.exception.usage is not None
        self.assertEqual(caught.exception.usage.completion_tokens, 8)
        diagnostic = json.loads(logs.records[0].getMessage())
        self.assertEqual(diagnostic["category"], "empty_completion")
        self.assertTrue(diagnostic["usage_present"])
        self.assertEqual(diagnostic["finish_reason"], "stop")
        self.assertEqual(diagnostic["content_chars"], 0)
        self.assertNotIn("private-question", str(logs.output))

    async def test_numeric_decoder_limit_survives_transport(self) -> None:
        from agent_provider import AgentRequest
        from stream_transport import attempt
        from packages.limits import LimitError

        session = Session()
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.aiohttp.ClientSession", return_value=session),
            patch(
                "stream_transport.StreamDecoder.feed",
                side_effect=LimitError(
                    "tool_limit", 16001, 16000, "argument characters"
                ),
            ),
            self.assertRaises(LimitError) as caught,
        ):
            _ = [
                e
                async for e in attempt(
                    AgentRequest(
                        messages=[{"role": "user", "content": "Explain"}],
                        tools=[],
                        timeout=5.0,
                    ),
                    ServerlessPolicy().models["text"],
                    5,
                )
            ]
        self.assertEqual(caught.exception.measured, 16001)
        self.assertEqual(caught.exception.limit, 16000)

    async def test_schema_size_limit_survives_without_echoing_provider_content(
        self,
    ) -> None:
        from agent_provider import AgentRequest
        from stream_transport import attempt
        from packages.limits import LimitError
        from streaming import FunctionDelta
        from pydantic import ValidationError

        try:
            FunctionDelta(name="private" * 12)
        except ValidationError as error:
            invalid = error
        session = Session()
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.aiohttp.ClientSession", return_value=session),
            patch("stream_transport.StreamDecoder.feed", side_effect=invalid),
            self.assertRaises(LimitError) as caught,
        ):
            _ = [
                e
                async for e in attempt(
                    AgentRequest(
                        messages=[{"role": "user", "content": "Explain"}],
                        tools=[],
                        timeout=5.0,
                    ),
                    ServerlessPolicy().models["text"],
                    5,
                )
            ]
        self.assertEqual((caught.exception.measured, caught.exception.limit), (84, 80))
        self.assertNotIn("private", caught.exception.detail)

    async def test_vision_none_is_explicit_and_empty_tools_are_omitted(
        self,
    ) -> None:
        from agent_provider import AgentRequest
        from stream_transport import attempt

        session = Session()
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.aiohttp.ClientSession", return_value=session),
        ):
            request = AgentRequest(
                messages=[{"role": "user", "content": "Describe."}],
                tools=[],
                timeout=5.0,
            )
            _ = [
                e
                async for e in attempt(
                    request, ServerlessPolicy().models["vision"], 5.0
                )
            ]
        assert isinstance(session.body, dict)
        self.assertNotIn("tools", session.body)
        self.assertEqual(session.body["reasoning_effort"], "none")

    async def test_failure_diagnostics_do_not_log_untrusted_payloads(self) -> None:
        from agent_provider import AgentRequest
        from stream_transport import attempt

        class InvalidContent(Content):
            async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
                yield b'data: {"secret": "private-provider-payload"}\n\n'

        for status in (429, 200):
            session = Session()
            session.response.status = status
            session.response.content = InvalidContent()
            with (
                patch.dict(os.environ, environment()),
                patch("stream_transport.aiohttp.ClientSession", return_value=session),
                self.assertLogs("stream_transport", level="WARNING") as logs,
            ):
                with self.assertRaisesRegex(RuntimeError, "provider_error"):
                    _ = [
                        event
                        async for event in attempt(
                            AgentRequest(
                                messages=[
                                    {"role": "user", "content": "private-question"}
                                ],
                                tools=[],
                                timeout=5.0,
                            ),
                            ServerlessPolicy().models["text"],
                            5.0,
                        )
                    ]
            diagnostic = json.loads(logs.records[0].getMessage())
            self.assertEqual(diagnostic["event"], "provider_stream_failure")
            self.assertEqual(diagnostic["content_chars"], 0)
            self.assertEqual(diagnostic["reasoning_chars"], 0)
            self.assertEqual(diagnostic["tool_calls"], 0)
            self.assertIsNone(diagnostic["finish_reason"])
            self.assertFalse(diagnostic["usage_present"])
            self.assertEqual(
                diagnostic["category"], "http" if status == 429 else "validation"
            )
            if status == 429:
                self.assertEqual(diagnostic["status"], 429)
            else:
                self.assertEqual(diagnostic["validation_types"], ["missing"])
            self.assertNotIn("private-provider-payload", str(logs.output))
            self.assertNotIn("private-question", str(logs.output))

    async def test_answer_only_preserves_tool_schemas_at_http_boundary(self) -> None:
        session = Session()
        tools = [{"type": "function", "function": {"name": "web_search"}}]
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.aiohttp.ClientSession", return_value=session),
        ):
            _ = [
                event
                async for event in AgentProvider().stream(
                    {
                        "messages": [{"role": "user", "content": "Bonjour"}],
                        "tools": tools,
                        "tool_choice": "none",
                        "timeout": 5.0,
                        "local_enabled": False,
                    }
                )
            ]
        assert isinstance(session.body, dict)
        self.assertEqual(session.body["tools"], tools)
        self.assertEqual(session.body["tool_choice"], "none")

    async def test_profile_finalization_has_no_active_tool_template(self) -> None:
        from agent_provider import AgentRequest
        from stream_transport import attempt
        from packages.tool_history import final_messages

        session = Session()
        messages: list[dict[str, object]] = [
            {"role": "user", "content": "What does the source say?"},
            {"role": "tool", "tool_call_id": "read-1", "content": "Full evidence."},
        ]
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.aiohttp.ClientSession", return_value=session),
        ):
            req = AgentRequest(
                messages=messages,
                tools=[{"type": "function", "function": {"name": "web_fetch"}}],
                profile="atlas-qwen",
                tool_choice="none",
                timeout=5.0,
            )
            _ = [e async for e in attempt(req, ServerlessPolicy().models["code"], 5.0)]
        assert isinstance(session.body, dict)
        self.assertNotIn("tools", session.body)
        self.assertNotIn("tool_choice", session.body)
        self.assertEqual(session.body["messages"], final_messages(messages))
        self.assertEqual(req.messages, messages)

    async def test_progress_and_close_before_terminal(self) -> None:
        session = Session()
        with (
            patch.dict(
                os.environ,
                environment(),
            ),
            patch("stream_transport.aiohttp.ClientSession", return_value=session),
        ):
            provider = AgentProvider()
            async with aclosing(
                provider.stream(
                    {
                        "messages": [{"role": "user", "content": "Bonjour"}],
                        "tools": [{}],
                        "timeout": 5.0,
                        "local_enabled": False,
                        "reasoning_effort": "low",
                    }
                )
            ) as events:
                self.assertEqual(
                    await events.__anext__(), {"delta": {"content": "Bonjour"}}
                )
                self.assertFalse(session.response.content.closed)
            # Closing outer iterator must close the inner HTTP context too.
            self.assertTrue(session.response.content.closed)
            assert isinstance(session.body, dict)
            self.assertEqual(session.body["max_tokens"], 2048)
            self.assertEqual(session.body["reasoning_effort"], "none")

    async def test_terminal_usage_is_validated(self) -> None:
        session = Session()
        with (
            patch.dict(
                os.environ,
                environment(),
            ),
            patch("stream_transport.aiohttp.ClientSession", return_value=session),
        ):
            events = [
                x
                async for x in AgentProvider().stream(
                    {
                        "messages": [{"role": "user", "content": "Bonjour"}],
                        "tools": [{}],
                        "timeout": 5.0,
                        "observe": True,
                    }
                )
            ]
            self.assertIn("result", events[-1])
            self.assertTrue(session.response.content.closed)

    async def test_task_routing_and_budget_before_transport(self) -> None:
        session = Session()
        with (
            patch.dict(os.environ, environment()),
            patch(
                "stream_transport.aiohttp.ClientSession", return_value=session
            ) as client,
        ):
            request = dict(
                messages=[{"role": "user", "content": "Write Python code"}],
                tools=[{}],
                timeout=5.0,
                observe=True,
                local_enabled=False,
            )
            events = [event async for event in AgentProvider().stream(request)]
            assert isinstance(session.body, dict)
            self.assertEqual(session.body["model"], ServerlessPolicy().models["code"])
            result = events[-1]["result"]
            assert isinstance(result, dict)
            self.assertEqual(result["observation"]["task_type"], "code")
            client.reset_mock()
            request["messages"] = [{"role": "user", "content": "x" * 300000}]
            with self.assertRaises(ValueError):
                _ = [event async for event in AgentProvider().stream(request)]
            client.assert_not_called()

    async def test_fallback_only_before_emission(self) -> None:
        from stream_transport import StreamRequest

        for emitted in (False, True):
            attempts: list[str] = []

            async def upstream(
                request: StreamRequest, model: str, timeout: float
            ) -> AsyncIterator[dict[str, object]]:
                attempts.append(model)
                if len(attempts) == 1:
                    if emitted:
                        yield {"delta": {"content": "first"}}
                    raise RuntimeError("provider_error")
                yield {
                    "result": {
                        "text": "ok",
                        "calls": [],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    }
                }

            with (
                patch.dict(os.environ, environment()),
                patch("stream_transport.attempt", upstream),
            ):
                payload = dict(
                    messages=[{"role": "user", "content": "Hello"}],
                    tools=[{}],
                    timeout=5.0,
                    observe=True,
                )
                if emitted:
                    with self.assertRaises(RuntimeError):
                        _ = [e async for e in AgentProvider().stream(payload)]
                    self.assertEqual(len(attempts), 1)
                else:
                    result = [e async for e in AgentProvider().stream(payload)][-1][
                        "result"
                    ]
                    assert isinstance(result, dict)
                    self.assertTrue(result["observation"]["fallback"])
                    policy = ServerlessPolicy()
                    self.assertEqual(
                        attempts, [policy.models["text"], policy.models["code"]]
                    )
