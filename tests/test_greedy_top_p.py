"""Greedy requests preserve top_p only where the provider requires it."""

import json
import os
import unittest
from unittest.mock import patch

from agent_provider import AgentProvider, AgentRequest
from packages.dev_request import Request
from serverless_support import environment


class GreedyTopPTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_transport_sends_required_top_p(self) -> None:
        from quality import stream_quality
        from test_hidden_profile_routing import Response, Session, _identity_retry

        session = Session([Response()])
        env = environment() | {
            "ATLAS_HIDDEN_PROFILES": "atlas-mistral",
            "MISTRAL_API_KEY": "test-only",
            "MISTRAL_PROFILE_MODEL": "mistral-large-4",
        }
        with (
            patch.dict(os.environ, env),
            patch("stream_transport.aiohttp.ClientSession", return_value=session),
            patch("provider_retry.retry_stream", side_effect=_identity_retry),
        ):
            request = AgentRequest(
                messages=[{"role": "user", "content": "private code question"}],
                tools=[],
                timeout=5.0,
                profile="atlas-mistral",
            )
            async for _ in stream_quality(AgentProvider(), request):
                pass

        body = session.calls[0][2]
        assert isinstance(body, dict)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["top_p"], 1)

    async def test_stream_transport_without_preset_is_unchanged(self) -> None:
        from quality import stream_quality
        from test_stream_transport import Session
        from test_hidden_profile_routing import _identity_retry

        session = Session()
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.aiohttp.ClientSession", return_value=session),
            patch("provider_retry.retry_stream", side_effect=_identity_retry),
        ):
            request = AgentRequest(
                messages=[{"role": "user", "content": "private question"}],
                tools=[],
                timeout=5.0,
                profile="atlas-qwen",
            )
            async for _ in stream_quality(AgentProvider(), request):
                pass

        assert isinstance(session.body, dict)
        self.assertEqual(session.body["temperature"], 0)
        self.assertNotIn("top_p", session.body)


class DevGatewayGreedyTopPTests(unittest.TestCase):
    def test_required_preset_sends_top_p(self) -> None:
        import dev_gateway

        request = Request.model_validate(
            {
                "messages": [{"role": "user", "content": "Reply OK"}],
                "temperature": 0,
                "top_p": 0.9,
            }
        )
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-only"}):
            plan = dev_gateway.prepare_api(request, "atlas-code", "key-test")

        body = json.loads(plan.body)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["top_p"], 1)
        self.assertNotIn("top_k", body)

    def test_other_preset_drops_top_p(self) -> None:
        import dev_gateway
        from api_routing import targets

        request = Request.model_validate(
            {
                "messages": [{"role": "user", "content": "Reply OK"}],
                "temperature": 0,
                "top_p": 0.9,
            }
        )
        with patch.dict(os.environ, {"SCW_GENERATIVE_API_KEY": "test-only"}):
            plan = dev_gateway.prepare_api(request, "atlas-code", "key-test")
            scaleway = next(
                target
                for target in targets("atlas-code", "none")[1]
                if target.provider == "scaleway"
            )
            plan = next(
                current
                for current in (plan, plan.fallback)
                if current and current.provider == scaleway.provider
            )

        body = json.loads(plan.body)
        self.assertEqual(body["temperature"], 0)
        self.assertNotIn("top_p", body)

    def test_absent_temperature_uses_schema_default_on_required_preset(self) -> None:
        import dev_gateway

        request = Request.model_validate(
            {
                "messages": [{"role": "user", "content": "Reply OK"}],
                "top_p": 0.9,
            }
        )
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-only"}):
            plan = dev_gateway.prepare_api(request, "atlas-code", "key-test")

        body = json.loads(plan.body)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["top_p"], 1)

    def test_absent_temperature_without_preset_drops_top_p(self) -> None:
        import dev_gateway
        from api_routing import targets

        request = Request.model_validate(
            {
                "messages": [{"role": "user", "content": "Reply OK"}],
                "top_p": 0.9,
            }
        )
        with patch.dict(os.environ, {"SCW_GENERATIVE_API_KEY": "test-only"}):
            plan = dev_gateway.prepare_api(request, "atlas-code", "key-test")
            scaleway = next(
                target
                for target in targets("atlas-code", "none")[1]
                if target.provider == "scaleway"
            )
            plan = next(
                current
                for current in (plan, plan.fallback)
                if current and current.provider == scaleway.provider
            )

        body = json.loads(plan.body)
        self.assertEqual(body["temperature"], 0)
        self.assertNotIn("top_p", body)
