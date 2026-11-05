"""Reasoning effort is an operator setting per model, off by default."""

from collections.abc import AsyncGenerator
from decimal import Decimal
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agent_provider import AgentProvider, AgentRequest
from provider_settings import presets, request_options
from quality import stream_quality
from serverless import ServerlessPolicy
from serverless_support import environment
from services.orchestrator.model import Configuration, GatewayModel


def request(**options: object) -> AgentRequest:
    return AgentRequest.model_validate(
        {
            "messages": [
                {"role": "user", "content": "Explain the Z80 transfer limit."}
            ],
            "tools": [],
            "timeout": 5.0,
            "local_enabled": False,
            "profile": "atlas-glm",
            **options,
        }
    )


def result(text: str, output: int, *, length: bool = False) -> dict[str, object]:
    return {
        "result": {
            "text": text,
            "calls": [],
            "usage": {"prompt_tokens": 20, "completion_tokens": output},
            "finish_reason": "length" if length else "stop",
            "reasoning": "Long trace." if length else "",
            "reasoning_tokens": output if length else 0,
        }
    }


def configured(efforts: dict[str, str]) -> dict[str, str]:
    return environment() | {"ATLAS_REASONING_EFFORTS": json.dumps(efforts)}


class PolicyTests(unittest.TestCase):
    def test_default_is_none_for_every_model(self) -> None:
        with patch.dict(os.environ, environment(), clear=True):
            self.assertEqual(set(ServerlessPolicy().efforts.values()), {"none"})
        with patch.dict(os.environ, {"ATLAS_PROVIDER": "hetzner"}, clear=True):
            self.assertEqual(set(ServerlessPolicy().efforts.values()), {"none"})

    def test_operator_setting_selects_one_model(self) -> None:
        with patch.dict(os.environ, configured({"glm-5.2": "medium"}), clear=True):
            efforts = ServerlessPolicy().efforts
        self.assertEqual(efforts["glm-5.2"], "medium")
        self.assertEqual(efforts["qwen3.5-397b-a17b"], "none")

    def test_preset_and_routing_capability_sources(self) -> None:
        hetzner = dict(presets()["hetzner"])
        hetzner["reasoning_efforts"] = {"Qwen/Qwen3.8-27B": "low"}
        with (
            patch.dict(presets(), {"hetzner": hetzner}),
            patch.dict(os.environ, {"ATLAS_PROVIDER": "hetzner"}, clear=True),
        ):
            self.assertEqual(ServerlessPolicy().efforts["Qwen/Qwen3.8-27B"], "low")
            with patch.dict(
                os.environ,
                {"ATLAS_REASONING_EFFORTS": '{"Qwen/Qwen3.8-27B": "none"}'},
            ):
                # The environment overrides the preset, including back to none.
                self.assertEqual(ServerlessPolicy().efforts["Qwen/Qwen3.8-27B"], "none")
        with patch.dict(os.environ, environment(), clear=True):
            policy = ServerlessPolicy.__new__(ServerlessPolicy)
            source = ServerlessPolicy()
            policy.config = json.loads(json.dumps(source.config))
            policy.models = dict(source.models)
            policy.config["text"]["capabilities"]["qwen3.5-397b-a17b"][
                "reasoning_effort"
            ] = "high"
            self.assertEqual(policy.reasoning_efforts()["qwen3.5-397b-a17b"], "high")

    def test_invalid_settings_fail_closed(self) -> None:
        cases = (
            (configured({"glm-5.2": "extreme"}), "invalid_reasoning_effort"),
            (configured({"unknown-model": "low"}), "unknown_reasoning_model"),
            (environment() | {"ATLAS_REASONING_EFFORTS": "[1]"}, "invalid_reasoning"),
            (environment() | {"ATLAS_REASONING_EFFORTS": "{"}, "invalid_reasoning"),
            (
                {
                    "ATLAS_PROVIDER": "mistral",
                    "ATLAS_REASONING_EFFORTS": '{"mistral-small-2603": "low"}',
                },
                "reasoning_unsupported",
            ),
        )
        for env, error in cases:
            with (
                self.subTest(error=error),
                patch.dict(os.environ, env, clear=True),
                self.assertRaisesRegex(ValueError, error),
            ):
                ServerlessPolicy()


class ProviderFieldTests(unittest.TestCase):
    def test_effort_field_per_provider(self) -> None:
        body = {"model": "m", "reasoning_effort": "medium"}
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(request_options(body)["reasoning_effort"], "medium")
            self.assertEqual(
                request_options(body, "scaleway")["reasoning_effort"], "medium"
            )
            thinking = request_options(
                body, "hetzner", role="text", configured_effort="medium"
            )
            self.assertEqual(
                thinking["chat_template_kwargs"], {"enable_thinking": True}
            )
            self.assertNotIn("reasoning_effort", thinking)
            # A client-supplied effort alone never enables the template switch.
            client = request_options(body, "hetzner", role="text")
            self.assertEqual(client["chat_template_kwargs"], {"enable_thinking": False})
            self.assertEqual(client["reasoning_effort"], "none")
            # Default none keeps today's Hetzner switch off and effort none.
            baseline = request_options(dict(body, reasoning_effort="none"), "hetzner")
            self.assertEqual(
                baseline["chat_template_kwargs"], {"enable_thinking": False}
            )
            self.assertEqual(baseline["reasoning_effort"], "none")
            self.assertNotIn("reasoning_effort", request_options(body, "mistral"))
        self.assertEqual(
            presets()["hetzner"]["request_options"]["chat_template_kwargs"],
            {"enable_thinking": False},
        )

    def test_clients_cannot_select_an_effort(self) -> None:
        self.assertEqual(request(reasoning_effort="low").reasoning_effort, "none")
        self.assertTrue(request(reasoning_disabled=True).reasoning_disabled)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_sends_the_attempt_effort(self) -> None:
        from stream_transport import attempt
        from test_stream_transport import Session

        for effort in ("none", "medium"):
            session = Session()
            with (
                self.subTest(effort=effort),
                patch.dict(os.environ, environment(), clear=True),
                patch("stream_transport.aiohttp.ClientSession", return_value=session),
            ):
                _ = [
                    event
                    async for event in attempt(
                        request().model_copy(update={"reasoning_effort": effort}),
                        "glm-5.2",
                        5.0,
                    )
                ]
            assert isinstance(session.body, dict)
            self.assertEqual(session.body["reasoning_effort"], effort)


class QualityTests(unittest.IsolatedAsyncioTestCase):
    async def run_quality(
        self, env: dict[str, str], replies: list[dict[str, object]], **options: object
    ) -> tuple[list[AgentRequest], dict[str, object]]:
        attempts: list[AgentRequest] = []

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            attempts.append(req)
            yield replies[len(attempts) - 1]

        with (
            patch.dict(os.environ, env, clear=True),
            patch("stream_transport.attempt", upstream),
        ):
            events = [
                e async for e in stream_quality(AgentProvider(), request(**options))
            ]
        final = events[-1]["result"]
        assert isinstance(final, dict)
        return attempts, final

    async def test_default_stays_none_end_to_end(self) -> None:
        attempts, final = await self.run_quality(environment(), [result("Answer.", 10)])
        self.assertEqual([a.reasoning_effort for a in attempts], ["none"])
        self.assertEqual(final["reasoning_effort"], "none")

    async def test_configured_model_reasons_and_reports_it(self) -> None:
        attempts, final = await self.run_quality(
            configured({"glm-5.2": "medium"}), [result("Answer.", 10)]
        )
        self.assertEqual([a.reasoning_effort for a in attempts], ["medium"])
        # Reasoning gets its own room on top of the requested answer length.
        self.assertEqual(attempts[0].max_tokens, 6000 + 6000)
        self.assertEqual(final["reasoning_effort"], "medium")
        self.assertFalse(final["reasoning_retried"])

    async def test_empty_length_retries_without_reasoning(self) -> None:
        attempts, final = await self.run_quality(
            configured({"glm-5.2": "high"}),
            [result("", 6000, length=True), result("Recovered answer.", 400)],
        )
        self.assertEqual([a.reasoning_effort for a in attempts], ["high", "none"])
        self.assertEqual(attempts[1].max_tokens, 3000)
        self.assertTrue(final["reasoning_retried"])
        self.assertEqual(final["reasoning_effort"], "none")
        self.assertEqual(final["text"], "Recovered answer.")

    async def test_recovered_answer_keeps_later_turns_off(self) -> None:
        attempts, final = await self.run_quality(
            configured({"glm-5.2": "medium"}),
            [result("Answer.", 10)],
            reasoning_disabled=True,
            max_tokens=3000,
        )
        self.assertEqual([a.reasoning_effort for a in attempts], ["none"])
        self.assertEqual(final["reasoning_effort"], "none")


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_used_effort_and_recovery_flag(self) -> None:
        model = GatewayModel(
            "http://127.0.0.1:1",
            Configuration(
                input_eur_per_mtok=Decimal("0.6"),
                output_eur_per_mtok=Decimal("3.6"),
                max_tokens=2048,
            ),
        )
        model.configure_quality("atlas-glm", 6000)
        wire = {
            "text": "",
            "calls": [{"id": "c", "name": "calculator", "arguments": '{"expr":"1"}'}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 100},
            "cost_eur": "0.001",
            "reasoning_effort": "medium",
        }
        retried = dict(wire, reasoning_effort="none", reasoning_retried=True)
        final = {
            "text": "Done.",
            "calls": [],
            "usage": {"prompt_tokens": 30, "completion_tokens": 10},
            "cost_eur": "0.001",
        }
        messages: list[dict[str, object]] = [{"role": "user", "content": "Compute."}]
        with patch.object(model, "post", new=AsyncMock()) as post:
            post.side_effect = [wire, retried, final]
            await model.complete(messages, 100)
            self.assertNotIn("reasoning_disabled", post.call_args.args[1])
            self.assertEqual(model.reasoning_used, "medium")
            await model.complete(messages, 100)
            await model.complete(messages, 100)
        sent = post.call_args.args[1]
        self.assertTrue(sent["reasoning_disabled"])
        self.assertEqual(sent["max_tokens"], 3000)
        self.assertEqual(model.reasoning_used, "medium")
        # The gateway accepts the orchestrator payload unchanged.
        AgentRequest.model_validate(
            {k: v for k, v in sent.items() if k != "messages"} | {"messages": messages}
        )

    def test_response_metadata_reports_used_effort(self) -> None:
        from fastapi.testclient import TestClient
        from services.orchestrator.chat_api import app
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.interactions import Interaction

        async def respond(request: ChatRequest, item: Interaction) -> None:
            item.reponse = "Complete answer"
            item.reasoning_effort = "medium"
            item.state = "done"

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            TestClient(app) as client,
            patch("services.orchestrator.chat_api.process", side_effect=respond),
        ):
            for streaming in (False, True):
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "atlas-glm",
                        "stream": streaming,
                        "messages": [{"role": "user", "content": "Explain this limit"}],
                    },
                )
                if streaming:
                    rows = [
                        json.loads(line[6:])
                        for line in response.text.splitlines()
                        if line.startswith("data: ") and line != "data: [DONE]"
                    ]
                    metadata = rows[-1]["atlas"]
                else:
                    metadata = response.json()["atlas"]
                self.assertEqual(metadata["reasoning_effort"], "medium")


if __name__ == "__main__":
    unittest.main()
