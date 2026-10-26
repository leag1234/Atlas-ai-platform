"""REQ-ENG-004: operator thinking selection is confined to the code role."""

import copy
from collections.abc import AsyncIterator
import os
import unittest
from unittest.mock import patch

from provider_settings import presets, request_options


class HetznerCodeThinkingTests(unittest.TestCase):
    def test_compose_forwards_operator_setting_without_enabling_it(self) -> None:
        from pathlib import Path

        import yaml

        compose = yaml.safe_load(Path("compose.yaml").read_text())
        self.assertEqual(
            compose["services"]["gateway"]["environment"]["ATLAS_CODE_THINKING"],
            "${ATLAS_CODE_THINKING:-}",
        )

    def test_code_thinking_defaults_to_enabled_and_does_not_mutate_shared_options(
        self,
    ) -> None:
        original = copy.deepcopy(presets()["hetzner"])
        body = {"model": original["text_model"], "reasoning_effort": "none"}
        with patch.dict(os.environ, {"ATLAS_PROVIDER": "hetzner"}, clear=True):
            baseline = request_options(body)
            enabled_by_default = request_options(body, role="code")
            self.assertEqual(
                enabled_by_default["chat_template_kwargs"], {"enable_thinking": True}
            )
            self.assertNotIn("reasoning_effort", enabled_by_default)
            with patch.dict(os.environ, {"ATLAS_CODE_THINKING": "true"}):
                enabled = request_options(body, role="code")
                self.assertEqual(
                    enabled["chat_template_kwargs"], {"enable_thinking": True}
                )
                self.assertNotIn("reasoning_effort", enabled)
                for role in (None, "text", "fast", "vision", "visionprevious"):
                    self.assertEqual(request_options(body, role=role), baseline)
            with patch.dict(os.environ, {"ATLAS_CODE_THINKING": "false"}):
                self.assertEqual(request_options(body, role="code"), baseline)
            with patch.dict(os.environ, {"ATLAS_CODE_THINKING": ""}):
                self.assertEqual(request_options(body, role="code"), enabled_by_default)
        self.assertEqual(presets()["hetzner"], original)
        self.assertEqual(body["reasoning_effort"], "none")

    def test_invalid_selection_fails_before_provider_dispatch(self) -> None:
        with patch.dict(os.environ, {"ATLAS_CODE_THINKING": "yes"}, clear=True):
            with self.assertRaisesRegex(ValueError, "invalid_code_thinking"):
                request_options({}, "hetzner", role="code")
            self.assertEqual(request_options({}, "scaleway", role="code"), {})
            self.assertEqual(request_options({}, "mistral", role="code"), {})


class HetznerCodeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_code_model_uses_text_fallback_and_records_actual_model(
        self,
    ) -> None:
        from agent_provider import AgentProvider, AgentRequest
        from quality import stream_quality
        from serverless import ServerlessPolicy
        from test_stream_transport import Session

        class ErrorContent:
            async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
                yield b'{"error":{"message":"model not found"}}'

        class ErrorResponse:
            def __init__(self, status: int) -> None:
                self.status = status
                self.content = ErrorContent()

            async def __aenter__(self) -> "ErrorResponse":
                return self

            async def __aexit__(self, *args: object) -> None:
                pass

        class ErrorSession:
            def __init__(self, status: int) -> None:
                self.body: dict[str, object] | None = None
                self.response = ErrorResponse(status)

            async def __aenter__(self) -> "ErrorSession":
                return self

            async def __aexit__(self, *args: object) -> None:
                pass

            def post(self, url: str, **kwargs: object) -> ErrorResponse:
                body = kwargs["json"]
                assert isinstance(body, dict)
                self.body = body
                return self.response

        for status in (400, 404):
            with self.subTest(status=status):
                unavailable = ErrorSession(status)
                served = Session()
                with patch.dict(os.environ, {"ATLAS_PROVIDER": "hetzner"}, clear=True):
                    policy = ServerlessPolicy()
                    request = AgentRequest(
                        messages=[{"role": "user", "content": "Write Python code"}],
                        tools=[],
                        timeout=5.0,
                        profile="atlas-glm",
                        observe=True,
                    )
                    with patch(
                        "stream_transport.aiohttp.ClientSession",
                        side_effect=[unavailable, served],
                    ):
                        events = [
                            event
                            async for event in stream_quality(AgentProvider(), request)
                        ]
                result = events[-1]["result"]
                assert isinstance(result, dict)
                assert unavailable.body is not None
                assert isinstance(served.body, dict)
                self.assertEqual(unavailable.body["model"], policy.models["code"])
                self.assertEqual(served.body["model"], policy.models["text"])
                self.assertEqual(result["provider_model"], policy.models["text"])
                self.assertTrue(result["observation"]["fallback"])

    async def test_code_timeout_uses_text_fallback_and_records_actual_model(
        self,
    ) -> None:
        from agent_provider import AgentProvider, AgentRequest
        from quality import stream_quality
        from serverless import ServerlessPolicy

        calls: list[str] = []

        async def fake_provider(
            request: AgentRequest, model: str, timeout: float
        ) -> AsyncIterator[dict[str, object]]:
            calls.append(model)
            if len(calls) == 1:
                raise TimeoutError("provider_timeout")
            yield {
                "result": {
                    "text": "A complete fallback answer.",
                    "calls": [],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 8},
                    "finish_reason": "stop",
                    "reasoning": "",
                    "reasoning_tokens": 0,
                }
            }

        with patch.dict(os.environ, {"ATLAS_PROVIDER": "hetzner"}, clear=True):
            policy = ServerlessPolicy()
            request = AgentRequest(
                messages=[{"role": "user", "content": "Write Python code"}],
                tools=[],
                timeout=5.0,
                profile="atlas-glm",
                observe=True,
            )
            with patch("stream_transport.attempt", fake_provider):
                events = [
                    event async for event in stream_quality(AgentProvider(), request)
                ]
        result = events[-1]["result"]
        assert isinstance(result, dict)
        self.assertEqual(calls, [policy.models["code"], policy.models["text"]])
        self.assertEqual(result["provider_model"], policy.models["text"])
        self.assertTrue(result["observation"]["fallback"])

    async def test_public_code_profile_passes_operator_switch_to_wire(self) -> None:
        from agent_provider import AgentRequest
        from serverless import ServerlessPolicy
        from stream_transport import attempt
        from test_stream_transport import Session

        with patch.dict(
            os.environ,
            {"ATLAS_PROVIDER": "hetzner", "ATLAS_CODE_THINKING": "true"},
            clear=True,
        ):
            policy = ServerlessPolicy()
            for profile, role in policy.public_profiles.items():
                session = Session()
                request = AgentRequest(
                    messages=[{"role": "user", "content": "Calculate a sum"}],
                    tools=[],
                    timeout=5.0,
                    profile=profile,
                )
                with patch(
                    "stream_transport.aiohttp.ClientSession", return_value=session
                ):
                    events = [e async for e in attempt(request, policy.models[role], 5)]
                body = session.body
                assert isinstance(body, dict)
                self.assertEqual(
                    body["chat_template_kwargs"], {"enable_thinking": role == "code"}
                )
                self.assertEqual(body["messages"], request.messages)
                self.assertEqual(body["max_tokens"], 6000)
                self.assertTrue(any("result" in event for event in events))

    async def test_code_profile_does_not_enable_thinking_for_vision_routing(
        self,
    ) -> None:
        from agent_provider import AgentRequest
        from serverless import ServerlessPolicy
        from stream_transport import attempt
        from test_stream_transport import Session

        with patch.dict(
            os.environ,
            {"ATLAS_PROVIDER": "hetzner", "ATLAS_CODE_THINKING": "true"},
            clear=True,
        ):
            policy = ServerlessPolicy()
            profile = next(
                p for p, role in policy.public_profiles.items() if role == "code"
            )
            request = AgentRequest(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.test/image.png"},
                            }
                        ],
                    }
                ],
                tools=[],
                timeout=5.0,
                profile=profile,
            )
            session = Session()
            with patch("stream_transport.aiohttp.ClientSession", return_value=session):
                events = [e async for e in attempt(request, policy.models["vision"], 5)]
            body = session.body
            assert isinstance(body, dict)
            self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
            self.assertTrue(any("result" in event for event in events))
