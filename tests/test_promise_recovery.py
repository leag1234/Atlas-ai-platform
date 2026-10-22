"""M26 D4: final answers deliver content after a failed terminal operation."""

from collections.abc import AsyncGenerator
import os
import unittest
from unittest.mock import patch

from agent_provider import AgentProvider, AgentRequest
from quality import stream_quality
from serverless_support import environment
from test_quality_gateway import request, result


class PromiseRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_failure_then_promise_delivers_content(self) -> None:
        promise = "Je vais creer un document DOCX avec trois conseils puis le publier."
        content = (
            "Le fichier n'a pas pu être produit.\n"
            "1. Envoyez un ordre du jour précis avant la réunion.\n"
            "2. Désignez une personne pour noter les décisions.\n"
            "3. Attribuez chaque action à une personne avec une date limite."
        )
        original = request(
            messages=[
                {"role": "user", "content": "Prépare trois conseils pour une réunion."},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "failed",
                            "type": "function",
                            "function": {
                                "name": "terminal_command",
                                "arguments": '{"command":"false"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "failed",
                    "content": '{"error":"terminal_operation_failed"}',
                },
            ],
            produces_files=True,
        )
        await self.recovered(original, promise, content)

    async def test_search_promise_without_failure_also_delivers_content(self) -> None:
        await self.recovered(
            request(),
            "I will search for evidence and then compare the findings.",
            "The supplied figures use different populations, so their averages are not directly comparable.",
        )

    async def recovered(
        self, original: AgentRequest, promise: str, content: str
    ) -> None:
        observed = []

        async def transport(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            observed.append(req)
            text = promise if len(observed) == 1 else content
            for char in text:
                yield {"delta": {"content": char}}
            yield result(text, 25)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", transport),
        ):
            events = [
                event async for event in stream_quality(AgentProvider(), original)
            ]
        final = events[-1]["result"]
        assert isinstance(final, dict)
        self.assertEqual(final["text"], content)
        self.assertEqual(len(observed), 2)
        self.assertEqual(observed[1].tool_choice, "none")
        self.assertEqual(final["provider_retries"], 1)
        self.assertEqual(final["usage"]["completion_tokens"], 50)
        self.assertEqual(
            "".join(
                str(delta.get("content", ""))
                for e in events
                if isinstance(delta := e.get("delta"), dict)
            ),
            content,
        )
        self.assertLessEqual(observed[1].timeout, original.timeout)

    async def test_failed_file_with_creation_script_requires_disclosure_and_content(
        self,
    ) -> None:
        await self.recovered(
            request(
                messages=[
                    {
                        "role": "user",
                        "content": "Create a DOCX with three meeting tips.",
                    },
                    {
                        "role": "tool",
                        "content": '{"error":"terminal_operation_failed"}',
                    },
                ],
                tools=[{"type": "function", "function": {"name": "terminal_command"}}],
                produces_files=True,
            ),
            "Je vais créer le document.\n```python\ncreate_document()\n```",
            "The file could not be produced. 1. Set an agenda. 2. Assign a note-taker. 3. Record action owners and deadlines.",
        )

    async def test_promise_with_actual_tool_call_is_allowed(self) -> None:
        calls = [
            {"id": "search", "name": "web_search", "arguments": '{"query":"evidence"}'}
        ]
        seen = []

        async def transport(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            seen.append(req)
            yield {
                "result": {
                    "text": "I will search for evidence.",
                    "calls": calls,
                    "usage": {"prompt_tokens": 20, "completion_tokens": 25},
                }
            }

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", transport),
        ):
            events = [e async for e in stream_quality(AgentProvider(), request())]
        final = events[-1]["result"]
        assert isinstance(final, dict)
        self.assertEqual(final["calls"], calls)
        self.assertEqual(len(seen), 1)

    async def test_second_promise_is_rejected_without_third_attempt(self) -> None:
        from packages.limits import ProviderFailure

        seen = []
        visible = []

        async def transport(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            seen.append(req)
            text = "I will search for the answer."
            yield {"delta": {"content": text}}
            yield result(text, 25)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", transport),
            self.assertRaises(ProviderFailure) as caught,
        ):
            async for event in stream_quality(AgentProvider(), request()):
                if "delta" in event:
                    visible.append(event)
        self.assertEqual(caught.exception.code, "promise_only_final_answer")
        self.assertEqual(caught.exception.retries, 1)
        self.assertEqual(len(seen), 2)
        self.assertEqual(visible, [])

    async def test_recovery_stops_at_original_deadline(self) -> None:
        seen = []

        async def transport(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            seen.append(timeout)
            yield result("I will search for an answer.", 25)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", transport),
            patch("quality.monotonic", side_effect=[0, 0, 6]),
            self.assertRaises(TimeoutError),
        ):
            _ = [e async for e in stream_quality(AgentProvider(), request(timeout=5))]
        self.assertEqual(len(seen), 1)

    async def test_recovery_reservation_stays_inside_remaining_budget(self) -> None:
        from decimal import Decimal
        from quality import input_bound
        from serverless import ServerlessPolicy
        from packages.limits import ProviderLimitError

        seen = []
        spent = Decimal(0)

        async def transport(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal spent
            seen.append(req)
            policy = ServerlessPolicy()
            self.assertLessEqual(
                spent
                + policy.cost(
                    model, input_bound(req.messages, req.tools), req.max_tokens
                ),
                Decimal("0.0003"),
            )
            spent += policy.cost(model, 20, 25)
            yield result("I will search for an answer.", 25)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", transport),
            self.assertRaises(ProviderLimitError),
        ):
            _ = [
                e
                async for e in stream_quality(
                    AgentProvider(), request(budget_eur="0.0003")
                )
            ]
        self.assertEqual(len(seen), 1)


class PromiseDetectionTests(unittest.TestCase):
    def test_intentions_across_languages_and_fragmented_prefixes(self) -> None:
        from answer_content import is_promise_only, possible_promise_answer

        for text in (
            "I will search for sources and compare them.",
            "I'll write the requested explanation.",
            "Je vais d'abord vérifier les documents, puis chercher des sources.",
            "JE VAIS CREER LE DOCUMENT.",
            "Ich werde die Quellen prüfen.",
            "Voy a buscar fuentes.",
            "Bien sûr. Je vais préparer le document.",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_promise_only(text))
                for end in range(1, len(text) + 1):
                    self.assertTrue(possible_promise_answer(text[:end]))

    def test_content_quotes_code_and_actual_tool_turns_are_not_promises(self) -> None:
        from answer_content import is_promise_only

        for text in (
            "I will explain. The two averages use different populations.",
            "Je vais expliquer :\n1. Fixez un ordre du jour.\n2. Notez les décisions.",
            'The sentence "I will search" expresses an intention.',
            '"I will search for sources."',
            "```python\nprint('I will search')\n```",
            "The meeting will start at noon.",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_promise_only(text))


class TerminalFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_terminal_failure_does_not_reopen_file_workflow(self) -> None:
        from decimal import Decimal
        import tempfile
        from unittest.mock import AsyncMock
        from services.orchestrator.chat_pipeline import process
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Result
        from services.orchestrator.model import Configuration, GatewayModel
        from services.orchestrator.terminal_client import TerminalClient

        model = GatewayModel(
            "http://127.0.0.1",
            Configuration(
                input_eur_per_mtok=Decimal("1"),
                output_eur_per_mtok=Decimal("1"),
                max_tokens=2048,
            ),
        )
        query = ChatRequest.model_validate(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "Create a DOCX with three meeting tips.",
                    }
                ]
            }
        )
        item = Interaction()
        content = "The file could not be produced. 1. Set an agenda. 2. Record decisions. 3. Assign action owners."

        async def failed_terminal_run(*args: object, **kwargs: object) -> Result:
            # A transport exception is recorded before TerminalClient increments
            # its command counter, as in the live harness's forced failure.
            item.erreurs.append("terminal_operation_failed")
            return Result(text=content, state="done", tool_calls=1)

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_TERMINAL_ENABLED": "1",
                    "ATLAS_WEB_CACHE": root + "/cache.sqlite",
                },
            ),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(TerminalClient, "initialize", AsyncMock()),
            patch(
                "services.orchestrator.chat_pipeline.run",
                AsyncMock(side_effect=failed_terminal_run),
            ) as run,
        ):
            await process(query, item)
        self.assertEqual(run.await_count, 1)
        self.assertEqual(item.reponse, content)
        self.assertEqual(item.files, [])
