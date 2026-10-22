"""Offline contracts for guest-mode requests to draw a simple subject."""

from decimal import Decimal
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services.orchestrator.appearance import (
    extract_svg,
    is_simple_drawing_request,
    simple_drawing_language,
)
from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.image_tool import finish_simple_drawing
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Result
from services.orchestrator.model import Observation


SVG = (
    '<svg viewBox="0 0 400 300"><rect width="400" height="300" fill="#bde7f5"/>'
    '<ellipse cx="200" cy="230" rx="120" ry="25" fill="#77b255"/>'
    '<ellipse cx="200" cy="155" rx="95" ry="65" fill="#fff8e7"/>'
    '<circle cx="275" cy="125" r="42" fill="#fff8e7"/>'
    '<circle cx="290" cy="118" r="5" fill="#333333"/>'
    '<ellipse cx="315" cy="140" rx="12" ry="8" fill="#e69aa8"/>'
    '<rect x="135" y="205" width="18" height="55" fill="#6b4f3b"/>'
    '<rect x="235" y="205" width="18" height="55" fill="#6b4f3b"/></svg>'
)


def drawing_request(text: str, lang: str = "en") -> ChatRequest:
    return ChatRequest.model_validate(
        {
            "lang": lang,
            "ui_locale": lang,
            "messages": [{"role": "user", "content": text}],
        }
    )


class DrawingRequestTests(unittest.IsolatedAsyncioTestCase):
    def test_five_language_detection_and_exclusions(self) -> None:
        positives = (
            "dessine moi un mouton",
            "draw a house",
            "zeichne einen Baum",
            "dibuja un gato",
            "disegna un logo for a bakery",
        )
        negatives = (
            "dessine un schéma du cycle",
            "draw a process chart",
            "zeichne ein Diagramm",
            "dibuja un diagrama",
            "disegna un processo",
            "dessine une image d'un chat",
            "explique comment dessiner un mouton",
        )
        self.assertTrue(all(is_simple_drawing_request(value) for value in positives))
        self.assertFalse(any(is_simple_drawing_request(value) for value in negatives))
        self.assertEqual(simple_drawing_language("draw a house", "fr"), "en")
        self.assertEqual(simple_drawing_language("zeichne einen Baum", "en"), "de")

    async def test_guest_route_has_no_research_or_tools_and_uses_bounded_budget(
        self,
    ) -> None:
        model = Mock(generation_ms=1.0, provider_retries=0, tools=[])
        model.input_tokens, model.output_tokens = 3, 4
        model.observations = [Observation(provider="local", route="simple")]
        result = Result(
            text="Here is a sheep.\n" + SVG,
            state="done",
            cost=Decimal("0.01"),
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_STABLE": "1",
                    "ATLAS_STABLE_IMAGES": "0",
                    "ATLAS_IMAGE_DIR": root,
                },
            ),
            patch(
                "services.orchestrator.chat_pipeline.GatewayModel.connect",
                new=AsyncMock(return_value=model),
            ),
            patch(
                "services.orchestrator.chat_pipeline.run",
                new=AsyncMock(return_value=result),
            ) as run,
            patch("services.orchestrator.chat_pipeline.required_research") as research,
        ):
            item = Interaction()
            await process(drawing_request("draw a sheep"), item)
            rendered_svg = Path(root).glob("*.svg").__next__().read_text()
        research.assert_not_called()
        self.assertEqual(model.tools, [])
        self.assertEqual(run.call_args.kwargs["initial_calls"], ())
        self.assertEqual(run.call_args.args[4].tool_calls, 0)
        self.assertEqual(run.call_args.args[4].cost, Decimal("0.10"))
        self.assertIn("/images/svg/", item.reponse)
        self.assertIn("?download=1", item.reponse)
        self.assertLess(
            item.reponse.index("little drawing"), item.reponse.index("/images/svg/")
        )
        self.assertLess(
            item.reponse.index("/images/svg/"),
            item.reponse.index("simplified illustration"),
        )
        self.assertIn('viewBox="0 0 400 300"', rendered_svg)

    async def test_image_request_keeps_localized_refusal(self) -> None:
        item = Interaction()
        with (
            patch.dict(os.environ, {"ATLAS_STABLE": "1", "ATLAS_STABLE_IMAGES": "0"}),
            patch(
                "services.orchestrator.chat_pipeline.GatewayModel.connect",
                new=AsyncMock(),
            ) as connect,
        ):
            await process(drawing_request("dessine une image d'un chat", "fr"), item)
        self.assertIn("génération d’images", item.reponse)
        connect.assert_not_awaited()

    async def test_short_english_drawing_overrides_a_french_ui_locale(self) -> None:
        model = Mock(generation_ms=1.0, provider_retries=0, tools=[])
        model.input_tokens, model.output_tokens = 3, 4
        model.observations = [Observation(provider="local", route="simple")]
        result = Result(text=SVG, state="done", cost=Decimal("0.01"))
        with (
            patch.dict(os.environ, {"ATLAS_STABLE": "1", "ATLAS_STABLE_IMAGES": "0"}),
            patch(
                "services.orchestrator.chat_pipeline.GatewayModel.connect",
                new=AsyncMock(return_value=model),
            ),
            patch(
                "services.orchestrator.chat_pipeline.run",
                new=AsyncMock(return_value=result),
            ) as run,
            patch(
                "services.orchestrator.image_tool.store_svg",
                return_value="/images/svg/x",
            ),
        ):
            item = Interaction()
            await process(drawing_request("draw a house", "fr"), item)
        self.assertEqual(run.call_args.args[0].lang, "en")
        self.assertIn("Gladly", item.reponse)

    async def test_invalid_drawing_only_offers_a_retry(self) -> None:
        item = Interaction()
        result = Result(text="```\n+--+\n```", state="done", cost=Decimal("0.01"))
        await finish_simple_drawing(drawing_request("draw a sheep"), item, result)
        self.assertIn("try again", item.reponse)
        self.assertNotIn("ASCII", item.reponse)
        self.assertNotIn("image", item.reponse.lower())

    def test_drawn_svg_sanitizer_rejects_active_and_external_content(self) -> None:
        for child in (
            "<script/>",
            '<rect onclick="run()"/>',
            '<rect fill="url(https://example.org/x)"/>',
        ):
            with self.subTest(child=child):
                result = Result(
                    text='<svg viewBox="0 0 400 300">' + child + "</svg>",
                    state="done",
                    cost=Decimal("0.01"),
                )
                self.assertIsNone(
                    extract_svg(result.text, canvas=(0.0, 0.0, 400.0, 300.0))
                )

    async def test_capability_instruction_only_applies_when_images_are_disabled(
        self,
    ) -> None:
        for stable_images, expected in (("0", True), ("1", False)):
            with self.subTest(stable_images=stable_images):
                model = Mock(generation_ms=1.0, provider_retries=0, tools=[])
                model.input_tokens, model.output_tokens = 3, 4
                model.observations = [Observation(provider="local", route="simple")]
                with (
                    patch.dict(
                        os.environ,
                        {"ATLAS_STABLE": "1", "ATLAS_STABLE_IMAGES": stable_images},
                    ),
                    patch(
                        "services.orchestrator.chat_pipeline.GatewayModel.connect",
                        new=AsyncMock(return_value=model),
                    ),
                    patch(
                        "services.orchestrator.chat_pipeline.run",
                        new=AsyncMock(
                            return_value=Result("Hi", "done", cost=Decimal("0.01"))
                        ),
                    ) as run,
                ):
                    await process(drawing_request("Hello"), Interaction())
                prompt = run.call_args.args[3]
                self.assertEqual("Stable guest capability limit" in prompt, expected)
