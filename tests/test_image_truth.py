"""Image policy facts come from configuration and override invented blocks."""

from decimal import Decimal
import unittest
from unittest.mock import AsyncMock, patch

from packages.image_policy import ImagePolicy
from services.orchestrator.image_truth import (
    policy_instruction,
    policy_statement,
    short_correction,
    unsupported_image_block,
)
from services.orchestrator.chat_pipeline import _process
from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.loop import Result
from services.orchestrator.stream_client import sink_context
from test_imagegen import png


class ImageTruthTests(unittest.IsolatedAsyncioTestCase):
    def test_actual_level_and_limits_are_in_pipeline_instruction(self) -> None:
        for level in ("standard", "strict"):
            with patch(
                "packages.image_limits.POLICY",
                ImagePolicy.model_validate({"image_safety_level": level}),
            ):
                statement = policy_instruction()
                self.assertIn(level, statement)
                for limit in (
                    "minors",
                    "real named people",
                    "identifiable people or groups",
                ):
                    self.assertIn(limit, statement)
                self.assertIn("no additional safety filter", statement)
                self.assertEqual(
                    "Strict image policy prohibits nudity" in statement,
                    level == "strict",
                )

    def test_false_claims_in_five_languages_and_truthful_guards(self) -> None:
        for reply in (
            "My image tool safety filters block childbirth.",
            "Les filtres de sécurité de mes outils d'image bloquent l'accouchement.",
            "Die Sicherheitsfilter verbieten Bilder von Geburten.",
            "Los filtros de seguridad prohíben imágenes del parto.",
            "I filtri di sicurezza vietano immagini del parto.",
            "I cannot create that image because of a technical restriction.",
            "Je ne peux pas créer cette image en raison de la politique de sécurité.",
        ):
            with self.subTest(reply=reply):
                self.assertTrue(
                    unsupported_image_block(reply, "Mary giving birth in a stable")
                )
        for reply in (
            "The image model does not block childbirth.",
            "No additional safety filter blocks childbirth.",
            "The GPU is busy (HTTP 409).",
            "A museum painting of a birth.",
            policy_statement(),
        ):
            self.assertFalse(
                unsupported_image_block(reply, "Mary giving birth in a stable")
            )
        self.assertFalse(
            unsupported_image_block(
                "The policy blocks this image", "a sexual scene involving a child"
            )
        )
        with patch(
            "packages.image_limits.POLICY", ImagePolicy(image_safety_level="strict")
        ):
            self.assertFalse(
                unsupported_image_block(
                    "The policy blocks this image", "a nude adult figure study"
                )
            )

    async def test_postcheck_replaces_claim_and_generates_with_original_request(
        self,
    ) -> None:
        request_text = "Please render an image of Mary giving birth to Jesus in Bethlehem's stable with Joseph and animals"
        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=100,
            ),
        )
        events: list[dict[str, object]] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        async def answer(*args: object, **kwargs: object) -> Result:
            self.assertIn("Running instance image policy", str(args))
            self.assertIsNone(model.sink)
            return Result(
                text="My image tool safety filters block childbirth.",
                state="done",
                cost=Decimal("0.001"),
            )

        with (
            patch.dict(
                "os.environ",
                {
                    "ATLAS_STABLE": "0",
                    "ATLAS_IMAGE_ON_DEMAND": "0",
                    "ATLAS_TERMINAL_ENABLED": "0",
                },
            ),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch("services.orchestrator.chat_pipeline.run", side_effect=answer),
            patch.object(
                GatewayModel,
                "post",
                AsyncMock(return_value={"image": png(), "cost_eur": 0.002}),
            ) as post,
        ):
            token = sink_context.set(sink)
            try:
                item = Interaction()
                await _process(
                    ChatRequest(
                        messages=[ChatMessage(role="user", content=request_text)],
                        lang="en",
                    ),
                    item,
                )
            finally:
                sink_context.reset(token)
        self.assertTrue(item.images)
        # The invented block is dropped silently: no policy text reaches the user.
        self.assertNotIn("safety level", item.reponse.lower())
        self.assertTrue(item.reponse.startswith("!["))
        self.assertNotIn("filters block childbirth", item.reponse)
        self.assertEqual(post.call_args.args[1]["prompt"], request_text)
        self.assertAlmostEqual(item.cout_eur, 0.003)
        streamed = "".join(str(event.get("delta", {})) for event in events)
        self.assertNotIn("safety level", streamed)
        self.assertNotIn("filters block childbirth", streamed)

    async def test_explanation_replaces_false_claim_without_generation_and_streams(
        self,
    ) -> None:
        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=100,
            ),
        )
        sink = AsyncMock()
        with (
            patch.dict(
                "os.environ", {"ATLAS_STABLE": "0", "ATLAS_TERMINAL_ENABLED": "0"}
            ),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch(
                "services.orchestrator.chat_pipeline.run",
                AsyncMock(
                    return_value=Result(
                        text="My image filters block childbirth.", state="done"
                    )
                ),
            ),
            patch.object(GatewayModel, "post", AsyncMock()) as post,
        ):
            token = sink_context.set(sink)
            try:
                item = Interaction()
                await _process(
                    ChatRequest(
                        messages=[
                            ChatMessage(
                                role="user",
                                content="Explain how image generation works",
                            )
                        ],
                        lang="en",
                    ),
                    item,
                )
            finally:
                sink_context.reset(token)
        post.assert_not_awaited()
        self.assertTrue(item.reponse.startswith(short_correction("en")))
        self.assertNotIn("safety level", item.reponse.lower())
        self.assertIn(
            {"delta": {"content": item.reponse}},
            [call.args[0] for call in sink.await_args_list],
        )
