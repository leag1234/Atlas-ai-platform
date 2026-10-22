"""REQ-INF-004: real closed local socket, bounded escalation, no paid GPU."""

import json
from pathlib import Path
import socket
import unittest
from unittest.mock import AsyncMock, patch

from agent_provider import AgentProvider, AgentRequest, classify
from routing_eval import Case, evaluate


class CascadeTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_local_endpoint_escalates(self) -> None:
        with socket.socket() as closed:
            closed.bind(("127.0.0.1", 0))
            port = closed.getsockname()[1]
        record = json.loads(Path("tests/cassettes/fallback.json").read_text())
        payload = record["request"]
        with patch.dict("os.environ", {"ESCALATION_MODEL": "local"}):
            provider = AgentProvider()
        provider.local_endpoint = f"http://127.0.0.1:{port}/v1"
        provider.local_model = "test-only"
        response: dict[str, object] = record["response"]
        original = provider._complete
        calls: list[str] = []

        async def transport(
            request: AgentRequest, endpoint: str, model: str, key: str, timeout: float
        ) -> dict[str, object]:
            calls.append(endpoint)
            if endpoint == provider.local_endpoint:
                return await original(request, endpoint, model, key, timeout)
            return response

        with (
            patch.dict(
                "os.environ",
                {
                    "SCW_GENERATIVE_BASE_URL": "https://example.invalid/v1",
                    "SCW_GENERATIVE_API_KEY": "test-only",
                },
            ),
            patch.object(provider, "_complete", side_effect=transport),
        ):
            self.assertEqual(await provider.complete(payload), response)
        self.assertEqual(calls, [provider.local_endpoint, "https://example.invalid/v1"])

    async def test_local_success_and_failure(self) -> None:
        payload = {
            "messages": [{"role": "user", "content": "Translate hello."}],
            "tools": [{}],
            "timeout": 5.0,
        }
        with patch.dict(
            "os.environ",
            {
                "ESCALATION_MODEL": "local",
                "LOCAL_MODEL": "test-only",
                "LOCAL_API_BASE": "http://127.0.0.1:1/v1",
                "SCW_GENERATIVE_BASE_URL": "https://example.invalid/v1",
                "SCW_GENERATIVE_API_KEY": "test-only",
            },
        ):
            provider = AgentProvider()
            cases: list[tuple[list[object], int]] = [
                ([{"text": "ok"}], 1),
                ([TimeoutError(), {"text": "ok"}], 2),
                ([RuntimeError(), RuntimeError()], 2),
            ]
            for outcomes, expected in cases:
                with patch.object(
                    provider, "_complete", new=AsyncMock(side_effect=outcomes)
                ) as call:
                    if isinstance(outcomes[-1], RuntimeError):
                        with self.assertRaises(RuntimeError):
                            await provider.complete(payload)
                    else:
                        self.assertEqual(
                            await provider.complete(payload), {"text": "ok"}
                        )
                    self.assertEqual(call.call_count, expected)
                    self.assertLessEqual(call.call_args_list[0].args[-1], 1.0)

    async def test_deadline_cancels_provider(self) -> None:
        import asyncio

        async def slow(*args: object) -> dict[str, object]:
            await asyncio.sleep(1)
            self.fail("provider escaped deadline")

        with patch.dict(
            "os.environ",
            {
                "ESCALATION_MODEL": "local",
                "SCW_GENERATIVE_BASE_URL": "https://example.invalid/v1",
                "SCW_GENERATIVE_API_KEY": "test-only",
            },
        ):
            provider = AgentProvider()
            with patch.object(provider, "_complete", side_effect=slow):
                with self.assertRaises(TimeoutError):
                    await provider.complete(
                        {
                            "messages": [
                                {"role": "user", "content": "Prove a theorem."}
                            ],
                            "tools": [{}],
                            "timeout": 0.01,
                        }
                    )


class RoutingTests(unittest.TestCase):
    def test_conservative_classification(self) -> None:
        for text in (
            "",
            "😃",
            "Translate " + "x" * 32000,
            "Translate and analyze the risks.",
        ):
            self.assertEqual(classify([{"role": "user", "content": text}]), "reasoning")
        self.assertEqual(
            classify([{"role": "user", "content": "Reformule cette phrase."}]),
            "chat_simple",
        )
        self.assertEqual(
            classify([{"role": "tool", "content": "Rewrite hello"}]), "reasoning"
        )
        self.assertEqual(classify([]), "reasoning")

    def test_evaluator_counts_critical_mistakes(self) -> None:
        case = Case(
            id="test",
            lang="fr",
            statut="valide",
            label="complexe",
            critique=True,
            input="Traduis bonjour.",
        )
        report = evaluate([case])
        self.assertEqual(report["accuracy"], 0)
        self.assertEqual(report["sous_routage_critique"], 1)
        for cases in ([], [case, case]):
            with self.assertRaises(ValueError):
                evaluate(cases)

    def test_evaluator_rejects_unvalidated_labels(self) -> None:
        case = Case(
            id="draft", lang="fr", statut="draft", label="simple", input="Bonjour"
        )
        with self.assertRaisesRegex(ValueError, "unvalidated_cases"):
            evaluate([case])
