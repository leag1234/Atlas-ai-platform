"""Hetzner research finalization and content-free failure diagnostics (M26 D5)."""

import copy
import json
import os
import unittest
from collections.abc import AsyncIterator
from unittest.mock import patch

from agent_provider import AgentProvider, AgentRequest
from packages.context_limit import ContextExceeded
from packages.limits import ProviderLimitError
from provider_retry import TransientProviderError
from provider_settings import request_options
from quality import stream_quality
from stream_transport import attempt
from test_stream_transport import Content, Session


def research_request() -> AgentRequest:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "Answer from the retrieved evidence."},
        {"role": "user", "content": "What caused yesterday's telecom outage?"},
    ]
    for index in range(9):
        name = "web_search" if index < 3 or index == 8 else "web_fetch"
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": str(index),
                        "type": "function",
                        "function": {"name": name, "arguments": "{}"},
                    }
                ],
            }
        )
        evidence = (
            '{"error": "tool_quota_exhausted", "reason": "search_limit"}'
            if index == 8
            else "Source: confirmed interruption; cause unknown. "
            * (450 if index >= 3 else 1)
        )
        messages.append(
            {"role": "tool", "tool_call_id": str(index), "content": evidence}
        )
    return AgentRequest(messages=messages, tools=[], profile="atlas-qwen", timeout=5.0)


class FinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_price_research_finalization_has_one_leading_system(
        self,
    ) -> None:
        session = Session()
        request = research_request()
        original = copy.deepcopy(request.messages)
        with (
            patch.dict(os.environ, {"ATLAS_PROVIDER": "hetzner"}, clear=True),
            patch("stream_transport.aiohttp.ClientSession", return_value=session),
        ):
            events = [e async for e in stream_quality(AgentProvider(), request)]
        body = session.body
        assert isinstance(body, dict)
        messages = body["messages"]
        self.assertEqual(
            [i for i, m in enumerate(messages) if m["role"] == "system"], [0]
        )
        self.assertIn("Answer from the retrieved evidence.", messages[0]["content"])
        self.assertIn("final answer", messages[0]["content"])
        self.assertEqual(messages[-1], original[1])
        histories = [
            json.loads(m["content"])["untrusted_tool_history"]
            for m in messages
            if m["role"] == "assistant"
        ]
        self.assertEqual(histories, original[2:])
        self.assertEqual(request.messages, original)
        self.assertEqual(body["max_tokens"], 6000)
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)
        assert isinstance(events[-1]["result"], dict)
        self.assertEqual(events[-1]["result"]["text"], "Bonjour")
        self.assertEqual(events[-1]["result"]["cost_eur"], "0")

    def test_system_content_parts_are_preserved_without_promoting_evidence(
        self,
    ) -> None:
        messages = [
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "Untrusted evidence"},
            {"role": "system", "content": [{"type": "text", "text": "Rule one"}]},
            {"role": "system", "content": "Rule two"},
        ]
        original = copy.deepcopy(messages)
        body = {"messages": messages}
        normalized = request_options(body, "hetzner")["messages"]
        self.assertEqual(normalized[0]["role"], "system")
        self.assertEqual(
            normalized[0]["content"],
            [
                {"type": "text", "text": "Rule one"},
                {"type": "text", "text": "Rule two"},
            ],
        )
        self.assertEqual(normalized[1:], original[:2])
        self.assertEqual(messages, original)
        self.assertEqual(request_options(body, "scaleway")["messages"], original)

    async def test_no_output_room_is_budget_failure_without_io_or_retry(self) -> None:
        for maximum in (0, -1):
            with self.subTest(maximum=maximum):
                request = research_request().model_copy(update={"max_tokens": maximum})
                with (
                    patch.dict(os.environ, {"ATLAS_PROVIDER": "hetzner"}, clear=True),
                    patch("stream_transport.aiohttp.ClientSession") as client,
                    self.assertRaises(ProviderLimitError) as caught,
                ):
                    _ = [e async for e in stream_quality(AgentProvider(), request)]
                self.assertEqual(caught.exception.code, "output_budget")
                self.assertEqual(caught.exception.measured, 1)
                self.assertEqual(caught.exception.limit, maximum)
                client.assert_not_called()

    async def test_full_context_is_local_context_refusal(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"ATLAS_PROVIDER": "hetzner", "ATLAS_MODEL_CONTEXT": "1000"},
                clear=True,
            ),
            patch("stream_transport.aiohttp.ClientSession") as client,
            self.assertRaises(ContextExceeded) as caught,
        ):
            _ = [e async for e in stream_quality(AgentProvider(), research_request())]
        self.assertGreater(caught.exception.tokens, caught.exception.limit)
        client.assert_not_called()

    async def test_http_diagnostics_select_bounded_message_only(self) -> None:
        cases: list[tuple[object, str | None]] = [
            (
                {"error": {"message": "System message must be at the beginning."}},
                "System message must be at the beginning.",
            ),
            ({"message": "x" * 400}, "x" * 300),
            ({"error": {"message": {"private": "request"}}}, None),
            ({"error": {"detail": "private-request"}}, None),
            ("not JSON", None),
            ({"error": {"message": "Rejected private-question"}}, "[redacted]"),
            ({"error": {"message": "Rejected sk-test-secret"}}, "[redacted]"),
            ({"error": {"message": "Visit https://private.example"}}, "[redacted]"),
            (
                {
                    "error": {
                        "message": "private-question https://private.example sk-test-secret"
                    }
                },
                "[redacted]",
            ),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected):

                class ErrorContent(Content):
                    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
                        yield json.dumps(payload).encode()

                session = Session()
                session.response.status = 400
                session.response.content = ErrorContent()
                request = AgentRequest(
                    messages=[{"role": "user", "content": "private-question"}],
                    tools=[],
                    profile="atlas-qwen",
                    timeout=5.0,
                )
                with (
                    patch.dict(
                        os.environ,
                        {
                            "ATLAS_PROVIDER": "hetzner",
                            "ATLAS_PROVIDER_API_KEY": "sk-test-secret",
                        },
                        clear=True,
                    ),
                    patch(
                        "stream_transport.aiohttp.ClientSession", return_value=session
                    ),
                    self.assertLogs("stream_transport", level="WARNING") as logs,
                    self.assertRaisesRegex(RuntimeError, "^provider_error$"),
                ):
                    _ = [e async for e in attempt(request, "test-model", 5.0)]
                diagnostic = json.loads(logs.records[0].getMessage())
                self.assertEqual(diagnostic["category"], "http")
                self.assertEqual(diagnostic["status"], 400)
                self.assertEqual(diagnostic.get("provider_message"), expected)
                for forbidden in (
                    "private-question",
                    "private.example",
                    "sk-test-secret",
                    "private-request",
                ):
                    self.assertNotIn(forbidden, str(logs.output))

    async def test_bad_error_bodies_keep_http_status_and_transient_classification(
        self,
    ) -> None:
        cases: list[list[bytes | TimeoutError]] = [
            [b"<html>private-request</html>"],
            [b"x" * 4096] * 5,
            [TimeoutError("private-request")],
        ]
        for pieces in cases:
            with self.subTest(pieces=len(pieces)):

                class ErrorContent(Content):
                    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
                        for piece in pieces:
                            if isinstance(piece, TimeoutError):
                                raise piece
                            yield piece

                session = Session()
                session.response.status = 503
                session.response.content = ErrorContent()
                with (
                    patch.dict(os.environ, {"ATLAS_PROVIDER": "hetzner"}, clear=True),
                    patch(
                        "stream_transport.aiohttp.ClientSession", return_value=session
                    ),
                    self.assertLogs("stream_transport", level="WARNING") as logs,
                    self.assertRaises(TransientProviderError),
                ):
                    _ = [
                        e async for e in attempt(research_request(), "test-model", 5.0)
                    ]
                diagnostic = json.loads(logs.records[0].getMessage())
                self.assertEqual(diagnostic["category"], "http")
                self.assertEqual(diagnostic["status"], 503)
                self.assertNotIn("provider_message", diagnostic)
                self.assertNotIn("private-request", str(logs.output))
