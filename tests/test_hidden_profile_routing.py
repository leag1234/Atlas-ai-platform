"""Hidden profiles stay opt-in and route to their declared provider."""

from collections.abc import AsyncIterator
from contextlib import aclosing
import json
import os
import unittest
from unittest.mock import patch

from typing import cast

import aiohttp

from agent_provider import AgentProvider, AgentRequest
from packages.profiles import context_window
from quality import profile_candidates
from serverless import ServerlessPolicy
from serverless_support import environment
from test_streaming_decoder import frame, end


HIDDEN_ENVIRONMENT = environment() | {
    "ATLAS_HIDDEN_PROFILES": "atlas-mistral",
    "MISTRAL_API_KEY": "test-only",
}


async def _identity_retry(
    attempt: object, *args: object
) -> AsyncIterator[dict[str, object]]:
    assert callable(attempt)
    async for event in attempt(*args):
        yield event


class Content:
    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.chunks = (
            chunks if chunks is not None else [frame({"content": "answer"}), end()]
        )

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


class Response:
    def __init__(self, status: int = 200, chunks: list[bytes] | None = None) -> None:
        self.status = status
        self.content = Content(chunks)

    async def __aenter__(self) -> "Response":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class Session:
    def __init__(self, responses: list[Response]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, object]] = []

    async def __aenter__(self) -> "Session":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def post(self, url: str, **kwargs: object) -> Response:
        headers = kwargs["headers"]
        assert isinstance(headers, dict)
        self.calls.append((url, str(headers["Authorization"]), kwargs["json"]))
        return self.responses.pop(0)


class PolicyTests(unittest.TestCase):
    def test_hidden_profile_is_disabled_by_default(self) -> None:
        with patch.dict(os.environ, environment()):
            policy = ServerlessPolicy()
        self.assertEqual(
            policy.configuration(),
            {
                "input_eur_per_mtok": "2.4",
                "output_eur_per_mtok": "9.1",
                "max_tokens": 2048,
                "gateway_reserves_quality": True,
            },
        )
        self.assertNotIn("mistral", policy.config)

    def test_enabled_hidden_profile_uses_its_own_provider(self) -> None:
        with patch.dict(os.environ, HIDDEN_ENVIRONMENT):
            policy = ServerlessPolicy()
            role = policy.profile_roles["atlas-mistral"]
            model = policy.models[role]
            self.assertEqual(model, "mistral-large-4")
            self.assertEqual(policy.config[role]["provider"], "mistral")
            self.assertEqual(policy.providers[model], "mistral")
            self.assertEqual(str(policy.prices[model]["input_eur_per_mtok"]), "0.595")
            self.assertEqual(str(policy.prices[model]["output_eur_per_mtok"]), "1.785")
            self.assertEqual(policy.efforts[model], "low")
            self.assertEqual(
                policy.configuration()["profile_reasoning_output_tokens"],
                {"atlas-mistral": 2000},
            )
            self.assertNotIn("reasoning_output_tokens", policy.configuration())
            self.assertEqual(context_window("atlas-mistral"), 1048576)
            self.assertEqual(context_window("atlas-mistral", vision=True), 262144)

    def test_visible_profile_candidates_exclude_hidden_role(self) -> None:
        with patch.dict(os.environ, HIDDEN_ENVIRONMENT):
            policy = ServerlessPolicy()
            hidden_role = policy.profile_roles["atlas-mistral"]
            hidden_model = policy.models[hidden_role]
            for profile in ("atlas-qwen", "atlas-glm", "atlas-deepseek"):
                with self.subTest(profile):
                    candidates = profile_candidates(policy, profile, "text")
                    self.assertNotIn(
                        hidden_model, [policy.models[role] for role in candidates]
                    )
            candidates = profile_candidates(policy, "atlas-mistral", "text")
            self.assertEqual(candidates[0], hidden_role)
            self.assertEqual(policy.models[candidates[0]], hidden_model)

    def test_provider_presets_do_not_replace_hidden_role(self) -> None:
        for provider in ("scaleway", "hetzner"):
            with (
                patch.dict(
                    os.environ,
                    HIDDEN_ENVIRONMENT
                    | {
                        "ATLAS_PROVIDER": provider,
                        "ATLAS_PROVIDER_API_KEY": "test-only",
                    },
                ),
                self.subTest(provider),
            ):
                policy = ServerlessPolicy(allow_unpriced=True)
                role = policy.profile_roles["atlas-mistral"]
                self.assertEqual(policy.config[role]["provider"], "mistral")
                self.assertEqual(policy.models[role], "mistral-large-4")

    def test_invalid_hidden_profile_and_missing_key(self) -> None:
        with patch.dict(
            os.environ, environment() | {"ATLAS_HIDDEN_PROFILES": "unknown"}
        ):
            with self.assertRaises(ValueError) as caught:
                ServerlessPolicy()
        self.assertIn("unknown_hidden_profile", str(caught.exception))

        with patch.dict(
            os.environ,
            environment()
            | {"ATLAS_HIDDEN_PROFILES": "atlas-mistral", "MISTRAL_API_KEY": ""},
        ):
            with self.assertRaises(ValueError) as caught:
                ServerlessPolicy()
        self.assertIn("provider_key_missing", str(caught.exception))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def collect(
        self, session: Session, request: AgentRequest
    ) -> list[dict[str, object]]:
        from quality import stream_quality

        events: list[dict[str, object]] = []
        with (
            patch("stream_transport.aiohttp.ClientSession", return_value=session),
            patch("provider_retry.retry_stream", side_effect=_identity_retry),
        ):
            async with aclosing(stream_quality(AgentProvider(), request)) as stream:
                async for event in stream:
                    events.append(event)
        return events

    def request(self, profile: str, **kwargs: object) -> AgentRequest:
        if "messages" in kwargs:
            messages = kwargs.pop("messages")
        else:
            messages = [
                {
                    "role": "user",
                    "content": "private code question"
                    if profile == "atlas-mistral"
                    else "private question",
                }
            ]
        return AgentRequest.model_validate(
            {
                "messages": messages,
                "tools": [],
                "timeout": 5.0,
                "profile": profile,
                **kwargs,
            }
        )

    async def test_hidden_profile_uses_mistral_endpoint_and_effort(self) -> None:
        session = Session([Response()])
        with patch.dict(os.environ, HIDDEN_ENVIRONMENT):
            await self.collect(
                session,
                self.request(
                    "atlas-mistral",
                    messages=[{"role": "user", "content": "write a Python function"}],
                ),
            )
        self.assertEqual(len(session.calls), 1)
        url, authorization, body = session.calls[0]
        self.assertEqual(url, "https://api.mistral.ai/v1/chat/completions")
        self.assertEqual(authorization, "Bearer test-only")
        assert isinstance(body, dict)
        self.assertEqual(body["model"], "mistral-large-4")
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertIn("prompt_cache_key", body)

    async def test_public_profiles_are_unchanged_when_hidden_enabled(self) -> None:
        for profile in ("atlas-qwen", "atlas-glm", "atlas-deepseek"):
            captures: list[tuple[str, str, str]] = []
            for hidden in ("", "atlas-mistral"):
                session = Session([Response()])
                env = environment() | {
                    "ATLAS_HIDDEN_PROFILES": hidden,
                    "MISTRAL_API_KEY": "test-only",
                }
                with patch.dict(os.environ, env):
                    await self.collect(session, self.request(profile))
                url, authorization, body = session.calls[0]
                captures.append((url, authorization, json.dumps(body, sort_keys=True)))
            self.assertEqual(captures[0], captures[1])

    async def test_hidden_primary_model_failure_falls_back_to_instance(self) -> None:
        session = Session(
            [
                Response(
                    404,
                    [json.dumps({"error": {"message": "model not found"}}).encode()],
                ),
                Response(),
            ]
        )
        with patch.dict(os.environ, HIDDEN_ENVIRONMENT):
            await self.collect(
                session,
                self.request(
                    "atlas-mistral",
                    messages=[{"role": "user", "content": "write a Python function"}],
                ),
            )
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(
            session.calls[0][0], "https://api.mistral.ai/v1/chat/completions"
        )
        self.assertEqual(
            session.calls[1][0], "https://example.invalid/v1/chat/completions"
        )
        self.assertEqual(session.calls[1][1], "Bearer test-only")
        assert isinstance(session.calls[1][2], dict)
        self.assertEqual(session.calls[1][2]["model"], "glm-5.2")

    async def test_strict_privacy_uses_only_owned_endpoint(self) -> None:
        session = Session([Response()])
        env = HIDDEN_ENVIRONMENT | {
            "ATLAS_OWNED_VLLM_BASE_URL": "https://owned.example.invalid/v1",
            "ATLAS_OWNED_VLLM_API_KEY": "owned-test-only",
        }
        with patch.dict(os.environ, env):
            await self.collect(
                session,
                self.request("atlas-mistral", privacy="strict", budget_eur="0.10"),
            )
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(
            session.calls[0][0], "https://owned.example.invalid/v1/chat/completions"
        )
        self.assertEqual(session.calls[0][1], "Bearer owned-test-only")

        session = Session([Response()])
        with (
            patch.dict(os.environ, HIDDEN_ENVIRONMENT),
            self.assertRaises(ValueError),
        ):
            await self.collect(
                session,
                self.request("atlas-mistral", privacy="strict", budget_eur="0.10"),
            )
        self.assertEqual(session.calls, [])

    async def test_provider_error_redacts_key(self) -> None:
        from stream_transport import provider_error_message

        response = Response(
            401,
            [json.dumps({"error": {"message": "invalid key test-only"}}).encode()],
        )
        with patch.dict(os.environ, HIDDEN_ENVIRONMENT):
            message = await provider_error_message(
                cast(aiohttp.ClientResponse, response),
                self.request("atlas-mistral"),
                "test-only",
            )
        self.assertEqual(message, "[redacted]")
