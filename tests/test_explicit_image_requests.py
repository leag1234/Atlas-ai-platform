"""Owner 2026-10-21: natural image instructions and guest drawing boundaries."""

import unittest
from unittest.mock import patch

from services.orchestrator.image_tool import explicit_image_request
from test_image_limits import Language


class ExplicitImageRequestTests(unittest.TestCase):
    def test_natural_requests_in_five_languages(self) -> None:
        examples = (
            "génère une image très réaliste de la Vierge Marie en train d'accoucher",
            "Generate a very realistic image of the Virgin Mary giving birth",
            "Erzeuge ein sehr realistisches Bild von der Jungfrau Maria bei der Geburt",
            "genera una imagen de la Virgen María dando a luz",
            "crea un'immagine della Vergine Maria mentre partorisce",
            "génère une photo de la Nativité",
            "génère une illustration de la Nativité",
            "fais-moi une image de la mer",
            "fais moi une photo de la mer",
            "create an image of a garden",
            "erzeuge ein Bild von einem Garten",
            "erzeuge eine Illustration von einem Garten",
            "crea un’immagine di un giardino",
            "crea una foto di un giardino",
            "CREATE AN IMAGE OF A GARDEN",
            "genere une image de la mer",
            "dessine un arbre",
            "dessine-moi un mouton",
            "draw a sheep",
            "zeichne einen Baum",
            "dibuja un árbol",
            "disegna un albero",
            "fais-moi un portrait d'un chat",
            "Make me a portrait of a cat",
            "Mach mir ein Porträt von einer Katze",
            "Hazme un retrato de un gato",
            "Fammi un ritratto di un gatto",
            "create a logo for a bakery",
            "crée un dessin d'un chat",
            "erzeuge ein Logo für eine Bäckerei",
            "crea un dibujo de un gato",
            "crea un poster per una panetteria",
            "je voudrais voir un dragon",
            "I would like to see a dragon",
        )
        with patch.dict("os.environ", {"ATLAS_STABLE": "0"}):
            for text in examples:
                with self.subTest(text=text):
                    self.assertTrue(explicit_image_request(text))

    def test_diagrams_and_non_requests_stay_outside_direct_route(self) -> None:
        examples = (
            "dessine un schéma du réseau",
            "dessine un schema du reseau",
            "draw a flowchart",
            "draw a chart of sales",
            "zeichne ein Diagramm",
            "dibuja un esquema",
            "disegna un grafico",
            "Explain the Nativity",
            "Describe this image",
            "What is an illustration?",
            "What is a portrait?",
            "How do I draw a portrait?",
            "Comment dessiner un portrait ?",
            "Wie zeichne ich ein Porträt?",
            "Cómo dibujar un retrato?",
            "Come disegnare un ritratto?",
            "write a portrait of my grandfather",
            "describe a portrait of my grandfather",
            "rédige un portrait de mon grand-père",
            "décris un portrait de mon grand-père",
            "schreibe ein Porträt meines Großvaters",
            "beschreibe ein Porträt meines Großvaters",
            "escribe un retrato de mi abuelo",
            "describe un retrato de mi abuelo",
            "scrivi un ritratto di mio nonno",
            "descrivi un ritratto di mio nonno",
            "portrait",
        )
        with patch.dict("os.environ", {"ATLAS_STABLE": "0"}):
            for text in examples:
                with self.subTest(text=text):
                    self.assertFalse(explicit_image_request(text))

    def test_guest_without_images_keeps_simple_drawing_route(self) -> None:
        with patch.dict(
            "os.environ", {"ATLAS_STABLE": "1", "ATLAS_STABLE_IMAGES": "0"}
        ):
            self.assertFalse(explicit_image_request("dessine-moi un mouton"))
            self.assertTrue(explicit_image_request("génère une image de la mer"))
        with patch.dict(
            "os.environ", {"ATLAS_STABLE": "1", "ATLAS_STABLE_IMAGES": "1"}
        ):
            self.assertTrue(explicit_image_request("dessine-moi un mouton"))


class DirectImageRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_childbirth_in_five_languages_never_asks_chat(self) -> None:
        from unittest.mock import AsyncMock
        from services.orchestrator.chat_pipeline import process
        from services.orchestrator.chat_schema import ChatMessage, ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.model import GatewayModel
        from test_imagegen import png

        examples: dict[Language, str] = {
            "fr": "génère une image très réaliste de la Vierge Marie en train d'accoucher",
            "en": "Generate a very realistic image of the Virgin Mary giving birth",
            "de": "Erzeuge ein sehr realistisches Bild von der Jungfrau Maria bei der Geburt",
            "es": "genera una imagen de la Virgen María dando a luz",
            "it": "crea un'immagine della Vergine Maria mentre partorisce",
        }
        for stable in ("0", "1"):
            for language, prompt in examples.items():
                with self.subTest(stable=stable, language=language):
                    with (
                        patch.dict(
                            "os.environ",
                            {
                                "ATLAS_STABLE": stable,
                                "ATLAS_STABLE_IMAGES": "1",
                                "ATLAS_IMAGE_ON_DEMAND": "0",
                            },
                        ),
                        patch.object(GatewayModel, "connect", AsyncMock()) as chat,
                        patch.object(
                            GatewayModel,
                            "post",
                            AsyncMock(return_value={"image": png(), "cost_eur": 0.002}),
                        ) as post,
                    ):
                        item = Interaction(question=prompt)
                        await process(
                            ChatRequest(
                                messages=[ChatMessage(role="user", content=prompt)],
                                ui_locale=language,
                            ),
                            item,
                        )
                    chat.assert_not_awaited()
                    post.assert_awaited_once()
                    self.assertTrue(post.call_args.args[0].endswith("/images/generate"))
                    self.assertEqual(post.call_args.args[1]["prompt"], prompt)
                    self.assertEqual(post.call_args.args[1]["lang"], language)
                    self.assertIn("![", item.reponse)
                    self.assertEqual(item.cout_eur, 0.002)
                    self.assertEqual(item.state, "done")

    async def test_benign_subjects_generate_and_limits_do_not(self) -> None:
        from unittest.mock import AsyncMock
        from services.orchestrator.chat_pipeline import _process
        from services.orchestrator.chat_schema import ChatMessage, ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.model import GatewayModel
        from packages.image_limits import refusal
        from test_imagegen import png
        from test_image_limits import ALLOWED, BLOCKED

        with patch.dict(
            "os.environ", {"ATLAS_STABLE": "0", "ATLAS_IMAGE_ON_DEMAND": "0"}
        ):
            for prompt in ALLOWED:
                with (
                    self.subTest(prompt=prompt),
                    patch.object(GatewayModel, "connect", AsyncMock()) as chat,
                    patch.object(
                        GatewayModel,
                        "post",
                        AsyncMock(return_value={"image": png(), "cost_eur": 0.001}),
                    ) as post,
                ):
                    item = Interaction()
                    await _process(
                        ChatRequest(
                            messages=[
                                ChatMessage(
                                    role="user", content="Create an image of " + prompt
                                )
                            ]
                        ),
                        item,
                    )
                    chat.assert_not_awaited()
                    post.assert_awaited_once()
                    self.assertIn("![", item.reponse)
            for lang, prompts in BLOCKED.items():
                for category, prompt in zip(
                    ("minors", "real_people", "incitement"), prompts
                ):
                    with (
                        self.subTest(lang=lang, category=category),
                        patch.object(GatewayModel, "connect", AsyncMock()) as chat,
                        patch.object(GatewayModel, "post", AsyncMock()) as post,
                    ):
                        item = Interaction()
                        await _process(
                            ChatRequest(
                                messages=[ChatMessage(role="user", content=prompt)],
                                lang=lang,
                            ),
                            item,
                        )
                        chat.assert_not_awaited()
                        post.assert_not_awaited()
                        self.assertEqual(item.reponse, refusal(category, lang))

    async def test_direct_route_emits_image_tool_events(self) -> None:
        from unittest.mock import AsyncMock
        from services.orchestrator.chat_pipeline import _process
        from services.orchestrator.chat_schema import ChatMessage, ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.model import GatewayModel
        from services.orchestrator.stream_client import sink_context
        from test_imagegen import png

        events: list[dict[str, object]] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        with (
            patch.dict(
                "os.environ", {"ATLAS_STABLE": "0", "ATLAS_IMAGE_ON_DEMAND": "0"}
            ),
            patch.object(GatewayModel, "connect", AsyncMock()) as chat,
            patch.object(
                GatewayModel,
                "post",
                AsyncMock(return_value={"image": png(), "cost_eur": 0.001}),
            ),
        ):
            token = sink_context.set(sink)
            try:
                await _process(
                    ChatRequest(
                        messages=[
                            ChatMessage(
                                role="user", content="Make me a portrait of a cat"
                            )
                        ]
                    ),
                    Interaction(),
                )
            finally:
                sink_context.reset(token)
        chat.assert_not_awaited()
        self.assertEqual(
            [
                event
                for event in events
                if str(event.get("phase", "")).startswith("tool_")
            ],
            [
                {"phase": "tool_started", "tool": "generate_image", "urls": []},
                {
                    "phase": "tool_finished",
                    "tool": "generate_image",
                    "urls": [],
                    "ok": True,
                },
            ],
        )

    async def test_direct_route_excludes_continuations_and_uploads(self) -> None:
        from unittest.mock import AsyncMock
        from services.orchestrator.chat_pipeline import _process
        from services.orchestrator.chat_schema import ChatMessage, ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.model import GatewayModel
        from test_vision_schema import picture

        uploaded = ChatMessage.model_validate(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Create an image of this"},
                    {"type": "image_url", "image_url": {"url": picture()}},
                ],
            }
        )
        with (
            patch(
                "services.orchestrator.chat_pipeline.process_vision", AsyncMock()
            ) as vision,
            patch(
                "services.orchestrator.chat_pipeline.process_image", AsyncMock()
            ) as generate,
        ):
            await _process(ChatRequest(messages=[uploaded]), Interaction())
            vision.assert_awaited_once()
            generate.assert_not_awaited()
        request = ChatRequest(
            messages=[ChatMessage(role="user", content="Create an image of a garden")],
            continuation=True,
        )
        with (
            patch.object(
                GatewayModel,
                "connect",
                AsyncMock(side_effect=RuntimeError("chat path")),
            ),
            patch(
                "services.orchestrator.chat_pipeline.process_image", AsyncMock()
            ) as generate,
        ):
            with self.assertRaisesRegex(RuntimeError, "chat path"):
                await _process(request, Interaction())
            generate.assert_not_awaited()

    async def test_iteration_keeps_original_subject_and_addition(self) -> None:
        from decimal import Decimal
        import json
        from unittest.mock import AsyncMock
        from services.orchestrator.chat_pipeline import process
        from services.orchestrator.chat_schema import ChatMessage, ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Call, Reservation, Turn
        from services.orchestrator.model import Configuration, GatewayModel
        from test_imagegen import png

        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=100,
            ),
        )
        decision = AsyncMock(
            return_value=Turn(
                "",
                (
                    Call(
                        "image",
                        "generate_image",
                        json.dumps({"prompt": "a cat with a hat"}),
                    ),
                ),
                Reservation(10, Decimal(0)),
            )
        )
        with (
            patch.dict(
                "os.environ", {"ATLAS_STABLE": "0", "ATLAS_IMAGE_ON_DEMAND": "0"}
            ),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(model, "complete", decision),
            patch.object(
                GatewayModel,
                "post",
                AsyncMock(return_value={"image": png(), "cost_eur": 0.002}),
            ) as generate,
        ):
            item = Interaction()
            await process(
                ChatRequest(
                    messages=[
                        ChatMessage(
                            role="user",
                            content="Create an image of a cat beside a red house",
                        ),
                        ChatMessage(
                            role="assistant", content="![Image](/images/example)"
                        ),
                        ChatMessage(role="user", content="Same but with a hat"),
                    ]
                ),
                item,
            )
        self.assertEqual(
            generate.call_args.args[1]["prompt"],
            "Create an image of a cat beside a red house Same but with a hat",
        )
        self.assertIn("![", item.reponse)
        self.assertEqual(item.cout_eur, 0.002)

    async def test_guest_without_images_returns_existing_message(self) -> None:
        from unittest.mock import AsyncMock
        from services.orchestrator.chat_pipeline import _process
        from services.orchestrator.chat_schema import ChatMessage, ChatRequest
        from services.orchestrator.image_tool import unavailable_reply
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.model import GatewayModel

        with (
            patch.dict("os.environ", {"ATLAS_STABLE": "1", "ATLAS_STABLE_IMAGES": "0"}),
            patch.object(GatewayModel, "connect", AsyncMock()) as chat,
            patch.object(GatewayModel, "post", AsyncMock()) as generate,
        ):
            item = Interaction()
            await _process(
                ChatRequest(
                    messages=[
                        ChatMessage(role="user", content="Create an image of a garden")
                    ],
                    lang="en",
                ),
                item,
            )
        chat.assert_not_awaited()
        generate.assert_not_awaited()
        self.assertEqual(item.reponse, unavailable_reply("en"))
