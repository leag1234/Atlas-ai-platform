"""No request path sends an empty tool list or a tool_choice without tools.

vLLM (the owned strict-mode server) answers HTTP 400 "tools must not be an
empty array"; serverless APIs tolerate it, which hid the defect.
"""

from collections.abc import AsyncIterator
import json
import os
import unittest
from unittest.mock import patch

from agent_provider import AgentProvider, AgentRequest
import provider_settings
from serverless import ServerlessPolicy
from serverless_support import environment
from stream_transport import attempt
from test_stream_transport import Content, Session

TOOLS = [
    {
        "type": "function",
        "function": {"name": "calculator", "parameters": {"type": "object"}},
    }
]
TOOL_KEYS = ("tools", "tool_choice", "parallel_tool_calls")


class Body(Content):
    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        yield json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "Paris."},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }
        ).encode()


def json_session() -> Session:
    session = Session()
    session.response.content = Body()
    return session


def request(**options: object) -> AgentRequest:
    return AgentRequest.model_validate(
        {
            "messages": [{"role": "user", "content": "Capital of France?"}],
            "tools": [],
            "timeout": 5.0,
            "local_enabled": False,
            **options,
        }
    )


def unset_provider() -> dict[str, str]:
    values = environment()
    values.pop("ATLAS_PROVIDER", None)
    return values


class RequestOptionsTests(unittest.TestCase):
    def test_every_provider_drops_empty_tools(self) -> None:
        with patch.dict(os.environ, unset_provider()):
            os.environ.pop("ATLAS_PROVIDER", None)
            for name in [None, "", *provider_settings.presets()]:
                for body in (
                    {"model": "m", "tools": []},
                    {"model": "m", "tools": [], "tool_choice": "auto"},
                    {"model": "m", "tool_choice": "none"},
                    {"model": "m", "tools": None, "parallel_tool_calls": False},
                ):
                    with self.subTest(provider=name, body=body):
                        original = dict(body)
                        sent = provider_settings.request_options(body, name)
                        for key in TOOL_KEYS:
                            self.assertNotIn(key, sent)
                        self.assertEqual(body, original)

    def test_every_provider_keeps_real_tools(self) -> None:
        with patch.dict(os.environ, unset_provider()):
            os.environ.pop("ATLAS_PROVIDER", None)
            for name in [None, "", *provider_settings.presets()]:
                with self.subTest(provider=name):
                    sent = provider_settings.request_options(
                        {"model": "m", "tools": TOOLS, "tool_choice": "auto"}, name
                    )
                    self.assertEqual(sent["tools"], TOOLS)
                    self.assertEqual(sent["tool_choice"], "auto")


class CompletePayloadTests(unittest.IsolatedAsyncioTestCase):
    async def sent(self, req: AgentRequest) -> dict[str, object]:
        session = json_session()
        with (
            patch.dict(os.environ, unset_provider()),
            patch("aiohttp.ClientSession", return_value=session),
        ):
            os.environ.pop("ATLAS_PROVIDER", None)
            await AgentProvider()._complete(
                req,
                "https://provider.example",
                ServerlessPolicy().models["text"],
                "",
                5.0,
            )
        assert isinstance(session.body, dict)
        return session.body

    async def test_complete_without_tools_omits_tool_fields(self) -> None:
        for options in (
            {},
            {"tool_choice": "none"},
            {"tool_choice": "auto"},
        ):
            with self.subTest(options=options):
                body = await self.sent(request(**options))
                for key in TOOL_KEYS:
                    self.assertNotIn(key, body)

    async def test_complete_with_tools_still_sends_them(self) -> None:
        body = await self.sent(request(tools=TOOLS, tool_choice="auto"))
        self.assertEqual(body["tools"], TOOLS)
        self.assertEqual(body["tool_choice"], "auto")

    async def test_owned_server_call_without_tools_omits_tool_fields(self) -> None:
        session = json_session()
        with (
            patch.dict(
                os.environ,
                unset_provider()
                | {"ATLAS_OWNED_VLLM_BASE_URL": "https://owned.example.invalid/v1"},
            ),
            patch("aiohttp.ClientSession", return_value=session),
        ):
            os.environ.pop("ATLAS_PROVIDER", None)
            await AgentProvider().owned_complete(request())
        assert isinstance(session.body, dict)
        for key in TOOL_KEYS:
            self.assertNotIn(key, session.body)


class StreamPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def sent(self, req: AgentRequest) -> dict[str, object]:
        session = Session()
        with (
            patch.dict(os.environ, unset_provider()),
            patch("stream_transport.aiohttp.ClientSession", return_value=session),
        ):
            os.environ.pop("ATLAS_PROVIDER", None)
            _ = [e async for e in attempt(req, ServerlessPolicy().models["text"], 5.0)]
        assert isinstance(session.body, dict)
        return session.body

    async def test_stream_without_tools_omits_tool_fields(self) -> None:
        body = await self.sent(request(tool_choice="auto"))
        for key in TOOL_KEYS:
            self.assertNotIn(key, body)

    async def test_stream_with_tools_still_sends_them(self) -> None:
        body = await self.sent(request(tools=TOOLS, tool_choice="auto"))
        self.assertEqual(body["tools"], TOOLS)
        self.assertEqual(body["tool_choice"], "auto")


if __name__ == "__main__":
    unittest.main()
