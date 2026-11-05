"""A widened reasoning attempt never makes its bounded recovery impossible."""

from collections.abc import AsyncGenerator
from decimal import Decimal
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agent_provider import AgentProvider, AgentRequest
from packages.limits import LimitError
from quality import input_bound, stream_quality
from serverless import ServerlessPolicy
from serverless_support import environment
from services.orchestrator.chat_pipeline import ChatTools, process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Result
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.terminal_client import TerminalClient
from test_configurable_reasoning import configured, request, result

STOP = (
    "output_budget: measured 12214 tokens including recovery; "
    "limit 9000 tokens including recovery"
)


async def run_quality(
    env: dict[str, str], replies: list[dict[str, object]], **options: object
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
        events = [e async for e in stream_quality(AgentProvider(), request(**options))]
    final = events[-1]["result"]
    assert isinstance(final, dict)
    return attempts, final


def gateway_model(env: dict[str, str]) -> GatewayModel:
    with patch.dict(os.environ, env, clear=True):
        configuration = Configuration.model_validate(ServerlessPolicy().configuration())
    model = GatewayModel("http://127.0.0.1:1", configuration)
    model.configure_quality("atlas-glm", 6000, has_attachments=True)
    return model


class RecoveryBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_reasoning_spent_on_length_then_recovery_answers(self) -> None:
        # Live case of 2026-11-04: medium effort, 12000 tokens of reasoning,
        # finish_reason=length, no answer; the recovery must still run.
        env = configured({"glm-5.2": "medium"})
        attempts, final = await run_quality(
            env, [result("", 12000, length=True), result("Recovered answer.", 214)]
        )
        self.assertEqual([a.max_tokens for a in attempts], [12000, 3000])
        self.assertEqual([a.reasoning_effort for a in attempts], ["medium", "none"])
        self.assertEqual(final["text"], "Recovered answer.")
        self.assertEqual(final["usage"]["completion_tokens"], 12214)  # type: ignore[index]

        model = gateway_model(env)
        self.assertEqual(model.configuration.reasoning_output_tokens, 6000)
        messages: list[dict[str, object]] = [{"role": "user", "content": "Summarize."}]
        reserved = model.estimate(messages)
        with patch.object(model, "post", new=AsyncMock(return_value=final)):
            turn = await model.complete(messages, 100)
        self.assertEqual(turn.text, "Recovered answer.")
        assert turn.usage is not None
        # The reservation covers the widened attempt plus the recovery.
        self.assertLessEqual(turn.usage.tokens, reserved.tokens)
        self.assertLessEqual(turn.usage.cost, reserved.cost)
        # Recovered turns run without reasoning and without the extra room.
        self.assertEqual(model.reasoning_room, 0)
        self.assertEqual(model.max_tokens, 3000)

    async def test_without_reasoning_the_limit_is_unchanged(self) -> None:
        env = environment()
        with patch.dict(os.environ, env, clear=True):
            self.assertNotIn(
                "reasoning_output_tokens", ServerlessPolicy().configuration()
            )
        model = gateway_model(env)
        self.assertEqual(model.reasoning_room, 0)
        messages: list[dict[str, object]] = [{"role": "user", "content": "Summarize."}]
        wire = {
            "text": "Answer.",
            "calls": [],
            "usage": {"prompt_tokens": 40, "completion_tokens": 12214},
            "cost_eur": "0.01",
        }
        with patch.object(model, "post", new=AsyncMock(return_value=wire)):
            with self.assertRaises(LimitError) as raised:
                await model.complete(messages, 100)
        self.assertEqual(raised.exception.limit, 9000)

    async def test_widening_keeps_the_recovery_affordable(self) -> None:
        env = configured({"glm-5.2": "medium"})
        with patch.dict(os.environ, env, clear=True):
            policy = ServerlessPolicy()
        sent = request(max_tokens=6000)
        incoming = input_bound(sent.messages, sent.tools)
        # The widened attempt alone fits; with the recovery it does not.
        budget = policy.cost("glm-5.2", incoming, 12000)
        attempts, final = await run_quality(
            env, [result("Answer.", 10)], max_tokens=6000, budget_eur=str(budget)
        )
        self.assertEqual(attempts[0].reasoning_effort, "medium")
        self.assertEqual(attempts[0].max_tokens, 6000)
        self.assertEqual(final["text"], "Answer.")


class InlineFallbackTests(unittest.IsolatedAsyncioTestCase):
    def test_no_content_above_claim_without_content(self) -> None:
        from services.orchestrator.file_results import inline_fallback

        for text in ("", "   \n"):
            notice = inline_fallback(text, "fr")
            self.assertNotIn("ci-dessus", notice)
            self.assertIn("aucun contenu", notice)
        self.assertNotIn("above", inline_fallback("", "en"))
        self.assertIn("ci-dessus", inline_fallback("Contenu de la synthèse.", "fr"))

    async def test_stopped_file_request_stays_an_honest_failure(self) -> None:
        model = GatewayModel(
            "http://127.0.0.1",
            Configuration(
                input_eur_per_mtok=Decimal("1"),
                output_eur_per_mtok=Decimal("1"),
                max_tokens=2048,
            ),
        )

        async def runner(*args: object, **kwargs: object) -> Result:
            return Result(
                text=STOP, state="stopped", reason="output_budget", cost=Decimal("0.02")
            )

        query = ChatRequest.model_validate(
            {
                "atlas_ui": True,
                "messages": [
                    {
                        "role": "user",
                        "content": "Crée un document Word qui résume la mission Apollo 11.",
                    }
                ],
            }
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {"ATLAS_TERMINAL_ENABLED": "1", "ATLAS_WEB_CACHE": root + "/cache"},
            ),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(TerminalClient, "initialize", AsyncMock()),
            patch.object(ChatTools, "execute", AsyncMock()),
            patch("services.orchestrator.chat_pipeline.run", side_effect=runner),
            patch(
                "services.orchestrator.content_recovery.recover_content",
                AsyncMock(return_value=False),
            ),
        ):
            item = Interaction()
            await process(query, item)
        self.assertNotEqual(item.state, "done")
        self.assertNotIn("ci-dessus", item.reponse)
        self.assertNotIn("solution de secours", item.reponse)
        self.assertNotIn("file_not_produced", item.erreurs)


if __name__ == "__main__":
    unittest.main()
