"""Offline coverage for physical-object illustration routing."""

from decimal import Decimal
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services.orchestrator.appearance import (
    is_appearance_request,
    legend_instruction,
    numbered_legend,
    technical_prompt,
)
from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.image_tool import declaration, finish_appearance
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Result
from services.orchestrator.model import Observation


class AppearanceRoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_five_language_positives_and_process_negatives(self) -> None:
        positives = (
            "À quoi ça ressemble, un moteur Stirling ?",
            "Les pièces d'une pompe",
            "What does a Stirling engine look like?",
            "Exploded view of a pump",
            "Wie sieht ein Stirlingmotor aus?",
            "Welche Teile drehen sich in einer Pumpe?",
            "¿Cómo es por dentro un motor Stirling?",
            "Vista explosionada de una bomba",
            "Com'è fatto un motore Stirling?",
            "Quali pezzi girano in una pompa?",
        )
        negatives = (
            "Explique le processus du moteur Stirling",
            "Compare a Stirling engine with a steam engine",
            "Erkläre den Prozess eines Stirlingmotors",
            "Vergleiche den Stirlingmotor mit einer Dampfmaschine",
            "Compara el proceso con un motor de vapor",
            "Explica el flujo de datos",
            "Confronta il processo con un motore a vapore",
            "Spiega l'architettura del sistema",
        )
        self.assertTrue(all(is_appearance_request(value) for value in positives))
        self.assertFalse(any(is_appearance_request(value) for value in negatives))

    def test_legend_uses_model_text_and_strips_ascii_or_mermaid_blocks(self) -> None:
        legend = numbered_legend(
            "Crankshaft: rotates.\n````\n+---+\n````\nPiston: oscillates.", "en"
        )
        self.assertIn("1. Crankshaft: rotates.", legend)
        self.assertIn("2. Piston: oscillates.", legend)
        self.assertNotIn("+---+", legend)
        self.assertIn("model-generated technical illustration", legend)
        self.assertIn("Numbers indicate the main parts", legend)
        self.assertIn("whether it is stationary", legend_instruction())
        prompt = technical_prompt("parts of a Stirling engine")
        self.assertIn("white background", prompt)
        self.assertIn("parts of a Stirling engine", prompt)
        self.assertIn("an exploded or cutaway view", prompt)
        self.assertNotRegex(
            prompt, r"(?i)\b(?:text|labels?|captions?|diagrams?|words?|callouts?)\b"
        )

    async def test_pipeline_routes_appearance_to_fake_image_gateway_and_legend(
        self,
    ) -> None:
        model = Mock(generation_ms=1.0, provider_retries=0)
        model.input_tokens, model.output_tokens = 3, 4
        model.observations = [Observation(provider="local", route="simple")]
        result = Result(
            text="Flywheel: rotates.\nPiston: oscillates.",
            state="done",
            cost=Decimal("0.01"),
        )
        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "What does it look like?"}]}
        )
        item = Interaction()
        with (
            patch(
                "services.orchestrator.chat_pipeline.GatewayModel.connect",
                new=AsyncMock(return_value=model),
            ),
            patch(
                "services.orchestrator.chat_pipeline.run",
                new=AsyncMock(return_value=result),
            ) as run,
            patch(
                "services.orchestrator.chat_pipeline.finish_appearance", new=AsyncMock()
            ) as finish,
        ):
            await process(request, item)
        finish.assert_awaited_once()
        self.assertEqual(finish.call_args.args[2], result)
        self.assertIsNone(model.sink)
        self.assertIn(legend_instruction(), run.call_args.args[3])

    async def test_finish_uses_fake_image_gateway_then_appends_a_legend(self) -> None:
        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "Parts of a Stirling engine"}]}
        )
        item = Interaction(reponse="![Generated image](/images/fake)")
        result = Result(
            text="Flywheel: rotates.\nPiston: oscillates.",
            state="done",
            cost=Decimal("0.01"),
        )
        with patch(
            "services.orchestrator.image_tool.process_image", new=AsyncMock()
        ) as generate:
            await finish_appearance(request, item, result, 0.0)
        self.assertIn("1. Flywheel: rotates.", item.reponse)
        self.assertIn("2. Piston: oscillates.", item.reponse)
        self.assertNotIn("```", item.reponse)
        self.assertIn("white background", generate.call_args.args[0])
        self.assertEqual(generate.call_args.kwargs["budget"], Decimal("0.05"))

    async def test_daily_budget_refusal_prevents_appearance_generation(self) -> None:
        from fastapi.testclient import TestClient
        from services.orchestrator.chat_api import app
        import os
        import tempfile

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_USER_DAILY_EUR": "0.01",
                    "ATLAS_DATA_DIR": root,
                    "ATLAS_INTERACTION_DIR": root + "/journal",
                },
                clear=False,
            ),
            TestClient(app) as client,
            patch("services.orchestrator.chat_api.process", new=AsyncMock()) as run,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "messages": [
                        {"role": "user", "content": "What does a pump look like?"}
                    ]
                },
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "budget_limit")
        run.assert_not_awaited()

    async def test_process_request_does_not_route_to_image_generation(self) -> None:
        model = Mock(generation_ms=1.0, provider_retries=0, tools=[])
        model.input_tokens, model.output_tokens = 3, 4
        model.observations = [Observation(provider="local", route="simple")]
        result = Result(
            text="```mermaid\nflowchart LR\nA --> B\n```",
            state="done",
            cost=Decimal("0.01"),
        )
        request = ChatRequest.model_validate(
            {
                "messages": [
                    {"role": "user", "content": "Explain the Stirling engine process"}
                ]
            }
        )
        item = Interaction()
        with (
            patch(
                "services.orchestrator.chat_pipeline.GatewayModel.connect",
                new=AsyncMock(return_value=model),
            ),
            patch(
                "services.orchestrator.chat_pipeline.run",
                new=AsyncMock(return_value=result),
            ) as run,
        ):
            await process(request, item)
        self.assertFalse(is_appearance_request(request.messages[-1].text))
        self.assertIn(declaration(), model.tools)
        self.assertNotIn(legend_instruction(), run.call_args.args[3])
        self.assertIn("```mermaid", item.reponse)

    async def test_stable_mode_uses_mechanical_route_without_image_generation(
        self,
    ) -> None:
        request = ChatRequest.model_validate(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "What does a Stirling engine look like?",
                    }
                ]
            }
        )
        result = Result(
            text="A useful text explanation.", state="done", cost=Decimal("0.01")
        )
        for stable_images, expected_finish in (("0", False), ("1", True)):
            with self.subTest(stable_images=stable_images):
                model = Mock(generation_ms=1.0, provider_retries=0, tools=[])
                model.input_tokens, model.output_tokens = 3, 4
                model.observations = [Observation(provider="local", route="simple")]
                item = Interaction()
                with (
                    patch.dict(
                        "os.environ",
                        {"ATLAS_STABLE": "1", "ATLAS_STABLE_IMAGES": stable_images},
                    ),
                    patch(
                        "services.orchestrator.chat_pipeline.GatewayModel.connect",
                        new=AsyncMock(return_value=model),
                    ),
                    patch(
                        "services.orchestrator.chat_pipeline.run",
                        new=AsyncMock(return_value=result),
                    ) as run,
                    patch(
                        "services.orchestrator.chat_pipeline.finish_appearance",
                        new=AsyncMock(),
                    ) as finish,
                    patch(
                        "services.orchestrator.chat_pipeline.finish_mechanical_drawing",
                        new=AsyncMock(),
                    ) as mechanical,
                ):
                    await process(request, item)
                self.assertEqual(finish.await_count, int(expected_finish))
                self.assertEqual(mechanical.await_count, int(not expected_finish))
                self.assertNotIn(declaration(), model.tools)
                self.assertEqual(
                    legend_instruction() in run.call_args.args[3], expected_finish
                )

    async def test_stable_mode_omits_image_tool_unless_explicitly_enabled(self) -> None:
        request = ChatRequest.model_validate(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "show me what a Stirling engine looks like, the parts",
                    }
                ]
            }
        )
        result = Result(
            text="A useful text explanation.", state="done", cost=Decimal("0.01")
        )
        for stable_images, offered in (("0", False), ("1", True)):
            with self.subTest(stable_images=stable_images):
                model = Mock(generation_ms=1.0, provider_retries=0, tools=[])
                model.input_tokens, model.output_tokens = 3, 4
                model.observations = [Observation(provider="local", route="simple")]
                with (
                    patch.dict(
                        "os.environ",
                        {"ATLAS_STABLE": "1", "ATLAS_STABLE_IMAGES": stable_images},
                    ),
                    patch(
                        "services.orchestrator.chat_pipeline.GatewayModel.connect",
                        new=AsyncMock(return_value=model),
                    ),
                    patch(
                        "services.orchestrator.chat_pipeline.run",
                        new=AsyncMock(return_value=result),
                    ),
                ):
                    await process(request, Interaction())
                self.assertEqual(declaration() in model.tools, offered)
