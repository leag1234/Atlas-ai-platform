"""Resolved language reaches the actual text and vision model requests."""

from decimal import Decimal
import json
import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Reservation, Turn
from services.orchestrator.model import Configuration, GatewayModel
from vision import VisionProvider
from test_vision_schema import picture


class ModelLanguageTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_and_followup_receive_resolved_language(self) -> None:
        for question in ("Et ensuite ?", "Résume ta réponse précédente."):
            model = GatewayModel(
                "http://unused",
                Configuration(
                    input_eur_per_mtok=Decimal(0),
                    output_eur_per_mtok=Decimal(0),
                    max_tokens=100,
                ),
            )
            complete = AsyncMock(
                return_value=Turn("Une réponse.", usage=Reservation(10, Decimal(0)))
            )
            with (
                patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
                patch.object(model, "complete", complete),
            ):
                await process(
                    ChatRequest(
                        ui_locale="en",
                        messages=[
                            ChatMessage(
                                role="user", content="Bonjour, discutons en français."
                            ),
                            ChatMessage(role="assistant", content="Bien sûr."),
                            ChatMessage(role="user", content=question),
                        ],
                    ),
                    Interaction(),
                )
            messages = complete.call_args.args[0]
            self.assertIn("Answer in French.", messages[0]["content"])
            self.assertEqual(json.loads(messages[-1]["content"])["lang"], "fr")

    async def test_vision_receives_explicit_french_for_short_unaccented_input(
        self,
    ) -> None:
        value = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "DECRIS CETTE IMAGE"},
                        {"type": "image_url", "image_url": {"url": picture()}},
                    ],
                }
            ]
        }
        recorded = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "Un carré rouge.", "tool_calls": []},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        provider = VisionProvider()
        with patch.object(provider, "post", AsyncMock(return_value=recorded)) as post:
            await provider.complete(value)
        self.assertIn(
            "Answer in French.", post.call_args.args[0]["messages"][0]["content"]
        )

    async def test_web_excerpt_preserves_model_selected_query(self) -> None:
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.tools import Runtime
        from services.orchestrator.loop import Call

        text = (
            "Python est un langage. " * 80
            + "The first public release was published in February 1991. " * 8
        )
        output = {"data": {"text": text}}
        tools = ChatTools(Interaction(), "Python langage")
        with patch.object(Runtime, "execute", AsyncMock(return_value=output)):
            result = await tools.execute(
                Call(
                    "read",
                    "web_fetch",
                    json.dumps(
                        {
                            "url": "https://example.org/history",
                            "query": "first public release February 1991",
                        }
                    ),
                ),
                10,
            )
        self.assertIn("1991", str(result))
