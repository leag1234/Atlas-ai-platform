"""Bounded sampling temperature and picture role map (pictures by code, task 1)."""

import json
import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from agent_provider import AgentProvider, AgentRequest
from packages.profiles import PICTURE_ROLES, picture_profile
from serverless import ServerlessPolicy
from serverless_support import environment
from test_hidden_profile_routing import (
    HIDDEN_ENVIRONMENT,
    Response,
    Session,
    _identity_retry,
)


def _request(**extra: object) -> AgentRequest:
    return AgentRequest(
        messages=[{"role": "user", "content": "fictional drawing request"}],
        tools=[],
        timeout=5.0,
        profile="atlas-qwen",
        **extra,  # type: ignore[arg-type]
    )


async def _body(request: AgentRequest, env: dict[str, str]) -> dict[str, object]:
    from quality import stream_quality

    session = Session([Response()])
    with (
        patch.dict(os.environ, env),
        patch("stream_transport.aiohttp.ClientSession", return_value=session),
        patch("provider_retry.retry_stream", side_effect=_identity_retry),
    ):
        async for _ in stream_quality(AgentProvider(), request):
            pass
    body = session.calls[0][2]
    assert isinstance(body, dict)
    return body


class SamplingFieldTests(unittest.TestCase):
    def test_unset_field_is_not_serialised(self) -> None:
        with patch.dict(os.environ, environment()):
            dumped = _request().model_dump()
        self.assertNotIn("sampling_temperature", dumped)

    def test_bounds_are_enforced(self) -> None:
        with patch.dict(os.environ, environment()):
            for value in (-0.1, 1.01, 2.0):
                with self.subTest(value=value), self.assertRaises(ValidationError):
                    _request(sampling_temperature=value)
            self.assertEqual(
                _request(sampling_temperature=0.7).sampling_temperature, 0.7
            )
            self.assertEqual(_request(sampling_temperature=1).sampling_temperature, 1)

    def test_legacy_calls_cannot_set_it(self) -> None:
        with (
            patch.dict(os.environ, environment()),
            self.assertRaisesRegex(ValidationError, "invalid_legacy_profile"),
        ):
            AgentRequest(
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
                timeout=5.0,
                sampling_temperature=0.5,
            )


class SamplingTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_body_is_unchanged(self) -> None:
        default = await _body(_request(), environment())
        explicit_none = await _body(_request(sampling_temperature=None), environment())
        self.assertEqual(
            json.dumps(default, sort_keys=True),
            json.dumps(explicit_none, sort_keys=True),
        )
        # Byte-identical to the body before the field existed: an integer zero.
        self.assertIs(type(default["temperature"]), int)
        self.assertEqual(default["temperature"], 0)
        self.assertNotIn("sampling_temperature", default)

    async def test_sampling_reaches_the_provider(self) -> None:
        body = await _body(_request(sampling_temperature=0.7), environment())
        self.assertEqual(body["temperature"], 0.7)
        self.assertNotIn("sampling_temperature", body)

    async def test_hidden_profile_keeps_top_p_with_sampling(self) -> None:
        env = HIDDEN_ENVIRONMENT | {"MISTRAL_PROFILE_MODEL": "mistral-large-4"}
        with patch.dict(os.environ, env):
            request = AgentRequest(
                messages=[{"role": "user", "content": "fictional drawing request"}],
                tools=[],
                timeout=5.0,
                profile="atlas-mistral",
                sampling_temperature=0.7,
            )
            body = await _body(request, env)
        self.assertEqual(body["temperature"], 0.7)
        self.assertEqual(body["top_p"], 1)


class PictureRoleTests(unittest.TestCase):
    def test_policy_reads_every_role(self) -> None:
        with patch.dict(os.environ, environment()):
            policy = ServerlessPolicy()
        self.assertEqual(set(policy.picture_roles), set(PICTURE_ROLES))

    def test_painter_needs_its_profile_enabled(self) -> None:
        with patch.dict(os.environ, environment()):
            os.environ.pop("ATLAS_HIDDEN_PROFILES", None)
            self.assertIsNone(picture_profile("picture_painter"))
            self.assertEqual(picture_profile("picture_checklist"), "atlas-deepseek")
        with patch.dict(os.environ, HIDDEN_ENVIRONMENT):
            self.assertEqual(picture_profile("picture_painter"), "atlas-mistral")

    def test_unknown_role_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown_picture_role"):
            picture_profile("picture_critic")


class GatewayModelPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_payload_carries_sampling_only_when_set(self) -> None:
        from services.orchestrator.model import GatewayModel

        payloads: list[dict[str, object]] = []

        async def post(url: str, payload: dict[str, object], timeout: float) -> object:
            payloads.append(dict(payload))
            raise RuntimeError("stop_after_payload")

        with patch.dict(os.environ, environment()):
            config = {
                "input_eur_per_mtok": "0.6",
                "output_eur_per_mtok": "3.6",
                "max_tokens": 2048,
            }
            from services.orchestrator.model import Configuration

            model = GatewayModel(
                "http://gateway.invalid", Configuration.model_validate(config)
            )
            model.tools = []
            model.configure_quality("atlas-qwen", 2000)
            with patch.object(GatewayModel, "post", side_effect=post):
                for value in (None, 0.7):
                    model.sampling_temperature = value
                    with self.assertRaisesRegex(RuntimeError, "stop_after_payload"):
                        await model.complete([{"role": "user", "content": "hi"}], 5)
        self.assertNotIn("sampling_temperature", payloads[0])
        self.assertEqual(payloads[1]["sampling_temperature"], 0.7)


if __name__ == "__main__":
    unittest.main()
