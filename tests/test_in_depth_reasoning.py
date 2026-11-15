"""An in-depth question thinks one reasoning level longer on a model configured to reason.

Measured 2026-11-15: a profile configured at the lowest reasoning level wrote in-depth answers
half as long as another profile (30 to 300 thinking tokens, never cut by the output budget);
one level more made them about 1.5 times longer and left short answers unchanged. The
request router marks the question (in_depth); bounded questions keep the configured level.
"""

from decimal import Decimal
import os
import unittest
from unittest.mock import AsyncMock, patch

from agent_provider import AgentRequest
from serverless import REASONING_OUTPUT_TOKENS, ServerlessPolicy, deeper
from serverless_support import environment
from services.orchestrator import request_router
from services.orchestrator.model import Configuration, GatewayModel
import test_configurable_reasoning as reasoning_tests
import test_request_router as router_tests
from test_configurable_reasoning import configured, result
from test_request_router import request, routed


class LevelTests(unittest.TestCase):
    def test_one_level_more_and_never_from_none(self) -> None:
        self.assertEqual(deeper("none"), "none")
        self.assertEqual(deeper("low"), "medium")
        self.assertEqual(deeper("medium"), "high")
        self.assertEqual(deeper("high"), "high")

    def test_policy_effort_and_rooms(self) -> None:
        with patch.dict(os.environ, configured({"glm-5.2": "low"}), clear=True):
            policy = ServerlessPolicy()
            configuration = policy.configuration()
        self.assertEqual(policy.effort("glm-5.2"), "low")
        self.assertEqual(policy.effort("glm-5.2", in_depth=True), "medium")
        # A model configured without reasoning stays without it.
        self.assertEqual(policy.effort("qwen3.5-397b-a17b", in_depth=True), "none")
        self.assertEqual(
            configuration["reasoning_output_tokens"], REASONING_OUTPUT_TOKENS["low"]
        )
        self.assertEqual(
            configuration["in_depth_reasoning_output_tokens"],
            REASONING_OUTPUT_TOKENS["medium"],
        )
        Configuration.model_validate(configuration)

    def test_no_configured_reasoning_adds_no_room(self) -> None:
        with patch.dict(os.environ, environment(), clear=True):
            configuration = ServerlessPolicy().configuration()
        self.assertNotIn("in_depth_reasoning_output_tokens", configuration)
        self.assertNotIn("profile_in_depth_reasoning_output_tokens", configuration)

    def test_legacy_call_cannot_ask_for_depth(self) -> None:
        with self.assertRaises(ValueError):
            AgentRequest.model_validate(
                {
                    "messages": [{"role": "user", "content": "x"}],
                    "tools": [],
                    "in_depth": True,
                }
            )


async def run_quality(
    env: dict[str, str], replies: list[dict[str, object]], **options: object
) -> tuple[list[AgentRequest], dict[str, object]]:
    """The quality harness of test_configurable_reasoning, without its own tests."""
    helper = reasoning_tests.QualityTests("test_default_stays_none_end_to_end")
    return await helper.run_quality(env, replies, **options)


class DepthQualityTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_depth_question_reasons_one_level_more(self) -> None:
        attempts, final = await run_quality(
            configured({"glm-5.2": "low"}), [result("Answer.", 10)], in_depth=True
        )
        self.assertEqual([a.reasoning_effort for a in attempts], ["medium"])
        self.assertEqual(
            attempts[0].max_tokens, 6000 + REASONING_OUTPUT_TOKENS["medium"]
        )
        self.assertEqual(final["reasoning_effort"], "medium")

    async def test_bounded_question_keeps_the_configured_level(self) -> None:
        attempts, _ = await run_quality(
            configured({"glm-5.2": "low"}), [result("Answer.", 10)]
        )
        self.assertEqual([a.reasoning_effort for a in attempts], ["low"])

    async def test_model_without_reasoning_stays_without(self) -> None:
        attempts, _ = await run_quality(
            environment(), [result("Answer.", 10)], in_depth=True
        )
        self.assertEqual([a.reasoning_effort for a in attempts], ["none"])

    async def test_ceiling_and_recovery_still_apply(self) -> None:
        attempts, _ = await run_quality(
            configured({"glm-5.2": "low"}),
            [result("Answer.", 10)],
            in_depth=True,
            reasoning_ceiling="low",
        )
        self.assertEqual([a.reasoning_effort for a in attempts], ["low"])
        attempts, final = await run_quality(
            configured({"glm-5.2": "low"}),
            [result("", 8000, length=True), result("Recovered answer.", 400)],
            in_depth=True,
        )
        self.assertEqual([a.reasoning_effort for a in attempts], ["medium", "none"])
        self.assertTrue(final["reasoning_retried"])


class DepthRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_keeps_the_answer_room(self) -> None:
        attempts, _ = await run_quality(
            configured({"glm-5.2": "low"}),
            [result("", 8000, length=True), result("Recovered answer.", 400)],
            in_depth=True,
        )
        self.assertEqual(attempts[1].max_tokens, 6000)
        attempts, _ = await run_quality(
            configured({"glm-5.2": "low"}),
            [result("", 2000, length=True), result("Recovered answer.", 400)],
        )
        self.assertEqual(attempts[1].max_tokens, 3000)


class OrchestratorDepthTests(unittest.IsolatedAsyncioTestCase):
    def model(self) -> GatewayModel:
        return GatewayModel(
            "http://127.0.0.1:1",
            Configuration(
                input_eur_per_mtok=Decimal("0.6"),
                output_eur_per_mtok=Decimal("3.6"),
                max_tokens=2048,
                reasoning_output_tokens=2000,
                in_depth_reasoning_output_tokens=6000,
            ),
        )

    async def test_payload_room_and_reset(self) -> None:
        model = self.model()
        model.configure_quality("atlas-glm", 6000)
        self.assertEqual(model.reasoning_room, 2000)
        model.in_depth = True
        self.assertEqual(model.reasoning_room, 6000)
        messages: list[dict[str, object]] = [{"role": "user", "content": "Why?"}]
        final = {
            "text": "Because.",
            "calls": [],
            "usage": {"prompt_tokens": 30, "completion_tokens": 10},
            "cost_eur": "0.001",
        }
        with patch.object(model, "post", new=AsyncMock(return_value=final)) as post:
            await model.complete(messages, 100)
        sent = post.call_args.args[1]
        self.assertIs(sent["in_depth"], True)
        AgentRequest.model_validate(
            {k: v for k, v in sent.items() if k != "messages"} | {"messages": messages}
        )
        # A new turn configuration starts bounded again.
        model.configure_quality("atlas-glm", 6000)
        self.assertFalse(model.in_depth)

    async def test_recovered_in_depth_turn_keeps_its_room(self) -> None:
        model = self.model()
        model.configure_quality("atlas-glm", 6000)
        model.in_depth = True
        recovered = {
            "text": "",
            "calls": [{"id": "c", "name": "calculator", "arguments": '{"expr":"1"}'}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 100},
            "cost_eur": "0.001",
            "reasoning_effort": "none",
            "reasoning_retried": True,
        }
        final = {
            "text": "Because.",
            "calls": [],
            "usage": {"prompt_tokens": 30, "completion_tokens": 10},
            "cost_eur": "0.001",
        }
        messages: list[dict[str, object]] = [{"role": "user", "content": "Why?"}]
        with patch.object(model, "post", new=AsyncMock()) as post:
            post.side_effect = [recovered, final]
            await model.complete(messages, 100)
            await model.complete(messages, 100)
        sent = post.call_args.args[1]
        self.assertEqual(sent["max_tokens"], 6000)
        self.assertTrue(sent["reasoning_disabled"])
        self.assertNotIn("in_depth", sent)

    async def test_bounded_turn_sends_no_field(self) -> None:
        model = self.model()
        model.configure_quality("atlas-glm", 6000)
        final = {
            "text": "Canberra.",
            "calls": [],
            "usage": {"prompt_tokens": 30, "completion_tokens": 3},
            "cost_eur": "0.0001",
        }
        with patch.object(model, "post", new=AsyncMock(return_value=final)) as post:
            await model.complete([{"role": "user", "content": "Capital?"}], 100)
        self.assertNotIn("in_depth", post.call_args.args[1])


class RouterDepthTests(unittest.TestCase):
    def test_field_is_parsed_and_defaults_to_false(self) -> None:
        self.assertTrue(
            request_router.parse('{"kind": "answer", "in_depth": true}').in_depth
        )
        self.assertFalse(request_router.parse('{"kind": "answer"}').in_depth)

    def test_prompt_states_the_field_in_every_example(self) -> None:
        system = request_router.messages("x", [])[0]["content"]
        self.assertIn("in_depth: true when", system)
        examples = system.split("Examples (last message, then the reply):")[1]
        self.assertEqual(examples.count("{"), examples.count('"in_depth"'))
        self.assertIn('"in_depth": true', examples)
        self.assertIn('"in_depth": false', examples)


class PipelineDepthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # The pipeline harness of test_request_router, without its own tests.
        self.helper = router_tests.PipelineGateTests("test_answer_stays_a_chat_answer")
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    async def test_in_depth_answer_marks_the_model(self) -> None:
        for kind in ("answer", "research"):
            with self.subTest(kind=kind):
                run, _, _, _ = await self.helper.process(
                    routed(
                        request("Why did the Roman Republic become an empire?"),
                        kind=kind,
                        in_depth=True,
                    )
                )
                self.assertIs(run.call_args.args[1].in_depth, True)

    async def test_bounded_answer_keeps_the_configured_level(self) -> None:
        run, _, _, _ = await self.helper.process(
            routed(request("What's the capital of Australia?"), kind="answer")
        )
        self.assertIs(run.call_args.args[1].in_depth, False)

    async def test_file_turn_is_not_marked(self) -> None:
        run, _, _, _ = await self.helper.process(
            routed(
                request("a spreadsheet to track my expenses"),
                kind="file",
                file_format="xlsx",
                in_depth=True,
            )
        )
        self.assertIs(run.call_args.args[1].in_depth, False)


if __name__ == "__main__":
    unittest.main()
