"""M18 transformations disable tools without altering ordinary conversation."""

import unittest

from packages.ports import atlas_ports
from unittest.mock import AsyncMock, patch
from decimal import Decimal

from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.followup import is_followup
from services.orchestrator.chat_pipeline import process
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Reservation, Turn
from services.orchestrator.model import Configuration, GatewayModel


class FollowupTests(unittest.IsolatedAsyncioTestCase):
    def test_requires_previous_answer_and_explicit_reference(self) -> None:
        for question, expected in [
            ("Traduis ta réponse précédente en allemand.", True),
            ("Plus court", True),
            ("Cherche la météo de demain", False),
            ("Traduis ce document", False),
        ]:
            messages = [
                ChatMessage(role="assistant", content="Previous answer"),
                ChatMessage(role="user", content=question),
            ]
            self.assertEqual(is_followup(messages), expected)
            self.assertFalse(is_followup(messages[-1:]))

    async def test_followup_preserves_answer_without_retrieval(self) -> None:
        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=100,
            ),
        )
        complete = AsyncMock(
            return_value=Turn(
                "Die Pflanzen nutzen Licht.", usage=Reservation(10, Decimal(0))
            )
        )
        with (
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(model, "complete", complete),
        ):
            item = Interaction()
            await process(
                ChatRequest(
                    messages=[
                        ChatMessage(
                            role="assistant",
                            content="Les plantes utilisent la lumière.",
                        ),
                        ChatMessage(
                            role="user",
                            content="Traduis ta réponse précédente en allemand.",
                        ),
                    ]
                ),
                item,
            )
        self.assertEqual(item.state, "done")
        self.assertEqual(model.tools, [])
        self.assertEqual(item.chunks_recuperes, [])
        self.assertIn("Les plantes", str(complete.call_args.args[0]))


class ImageHistoryTests(unittest.TestCase):
    def test_legacy_base64_removed_before_context_limit(self) -> None:
        message = ChatMessage(
            role="assistant",
            content="![image](data:image/png;base64," + "A" * 40000 + ")",
        )
        self.assertNotIn("base64", message.text)
        self.assertLess(len(message.text), 100)

    def test_iteration_only_on_previous_image(self) -> None:
        from services.orchestrator.followup import image_iteration

        messages = [
            ChatMessage(role="user", content="Generate an image of a cat"),
            ChatMessage(
                role="assistant",
                content="![Image]("
                + atlas_ports().url("adapter", "localhost")
                + "/images/abc)",
            ),
            ChatMessage(role="user", content="Ajoute des patins"),
        ]
        self.assertIn("cat", image_iteration(messages) or "")
        self.assertIn("patins", image_iteration(messages) or "")
        messages[-2] = ChatMessage(role="assistant", content="A text answer")
        self.assertIsNone(image_iteration(messages))

    def test_multiple_iterations_preserve_natural_original_request(self) -> None:
        from services.orchestrator.followup import image_iteration

        messages = [
            ChatMessage(role="user", content="dessine-moi un mouton à cinq pattes"),
            ChatMessage(
                role="assistant", content="![Image](http://localhost/images/abc)"
            ),
            ChatMessage(role="user", content="Ajoute des lunettes bleues"),
            ChatMessage(
                role="assistant", content="![Image](http://localhost/images/def)"
            ),
            ChatMessage(role="user", content="Ajoute un chapeau"),
        ]
        prompt = image_iteration(messages) or ""
        for constraint in ("mouton", "cinq pattes", "lunettes bleues", "chapeau"):
            self.assertIn(constraint, prompt)


class ImageHistoryVisionPipelineTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def vision_reply(text: str = "TABLE FACT: 17, 25, 8") -> dict[str, object]:
        return {
            "text": text,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "cost_eur": "0.01",
            "observation": {"provider": "escalade", "route": "complexe"},
        }

    @staticmethod
    def image_message(text: str, url: str) -> ChatMessage:
        return ChatMessage.model_validate(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        )

    async def test_followup_sends_history_image_to_profiled_vision(self) -> None:
        from test_vision_schema import picture

        request = ChatRequest(
            messages=[
                self.image_message("lis ce tableau", picture()),
                ChatMessage(role="assistant", content="Je vais le lire."),
                ChatMessage(
                    role="user", content="quelle est la somme de la deuxième colonne ?"
                ),
            ]
        )
        with patch.object(
            GatewayModel, "post", AsyncMock(return_value=self.vision_reply())
        ) as post:
            item = Interaction()
            await process(request, item)
        self.assertEqual(item.reponse, "TABLE FACT: 17, 25, 8")
        self.assertEqual(item.task_type, "vision")
        assert post.await_args is not None
        payload = post.await_args.args[1]
        self.assertEqual(payload["lang"], "fr")
        self.assertEqual(
            sum(
                part["type"] == "image_url"
                for part in payload["messages"][0]["content"]
            ),
            1,
        )

    async def test_two_history_images_are_retained(self) -> None:
        from test_vision_schema import picture

        request = ChatRequest(
            messages=[
                self.image_message("compare les tableaux", picture()),
                ChatMessage(role="assistant", content="D'accord."),
                self.image_message("et celui-ci", picture()),
                ChatMessage(role="assistant", content="D'accord."),
                ChatMessage(role="user", content="lequel a le plus grand total ?"),
            ]
        )
        with patch.object(
            GatewayModel, "post", AsyncMock(return_value=self.vision_reply())
        ) as post:
            await process(request, Interaction())
        assert post.await_args is not None
        messages = post.await_args.args[1]["messages"]
        self.assertEqual(
            sum(
                part["type"] == "image_url"
                for message in messages
                if isinstance(message["content"], list)
                for part in message["content"]
            ),
            2,
        )

    async def test_image_research_uses_untrusted_vision_evidence_and_tools(
        self,
    ) -> None:
        from test_vision_schema import picture
        from services.orchestrator.chat_pipeline import ChatTools

        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=100,
            ),
        )

        async def complete(messages: object, timeout: float) -> Turn:
            self.assertIn("untrusted_image_description", str(messages))
            self.assertIn("TABLE FACT", str(messages))
            return Turn("Tool-backed answer", usage=Reservation(1, Decimal(0)))

        request = ChatRequest(
            messages=[
                self.image_message(
                    "calcule la somme et cherche sur le web la dernière version stable de Python",
                    picture(),
                )
            ]
        )
        with (
            patch.object(
                GatewayModel, "post", AsyncMock(return_value=self.vision_reply())
            ) as vision,
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(model, "complete", side_effect=complete),
            patch.object(
                ChatTools, "execute", AsyncMock(return_value={"data": {"results": []}})
            ) as execute,
        ):
            item = Interaction()
            await process(request, item)
        self.assertEqual(item.reponse, "Tool-backed answer")
        self.assertEqual(item.cout_eur, 0.01)
        vision.assert_awaited_once()
        execute.assert_awaited_once()

    async def test_image_research_budget_failure_is_localized(self) -> None:
        from test_vision_schema import picture
        from services.orchestrator.model import GatewayError

        request = ChatRequest(
            messages=[
                self.image_message(
                    "calcule et cherche sur le web la dernière version stable",
                    picture(),
                )
            ]
        )
        with patch.object(
            GatewayModel,
            "post",
            AsyncMock(
                side_effect=GatewayError(
                    "cost_budget",
                    429,
                    "Reservation 100001 microEUR; limit 100000 microEUR",
                )
            ),
        ):
            item = Interaction()
            await process(request, item)
        self.assertIn("Je n’ai pas pu voir l’image", item.reponse)
        self.assertIn("100001 microEUR", item.reponse)
        self.assertEqual(item.images[0]["format"], "PNG")

    async def test_text_only_history_keeps_the_existing_general_request(self) -> None:
        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=100,
            ),
        )
        complete = AsyncMock(
            return_value=Turn("Réponse inchangée", usage=Reservation(1, Decimal(0)))
        )
        request = ChatRequest(
            messages=[
                ChatMessage(role="assistant", content="Réponse précédente."),
                ChatMessage(role="user", content="Plus court"),
            ]
        )
        with (
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(model, "complete", complete),
            patch.object(GatewayModel, "post", AsyncMock()) as vision,
        ):
            await process(request, Interaction())
        vision.assert_not_awaited()
        assert complete.await_args is not None
        messages = complete.await_args.args[0]
        self.assertEqual(
            messages[1], {"role": "assistant", "content": "Réponse précédente."}
        )
        self.assertNotIn("untrusted_image_description", str(messages))


class WebRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_search_retried_with_same_ledger(self) -> None:
        from services.orchestrator.loop import Call, Limits, Query, run
        from test_budgets import Model, Tools

        for limit, expected in [(10, "done"), (1, "stopped")]:
            model, tools = Model(), Tools()
            complete = AsyncMock(
                side_effect=[
                    Turn(
                        "",
                        (
                            Call(
                                "search", "web_search", '{"query":"topic","lang":"en"}'
                            ),
                        ),
                    ),
                    Turn("A sourced answer"),
                ]
            )
            execute = AsyncMock(
                side_effect=[{"error": "timeout"}, {"data": {"results": []}}]
            )
            with (
                patch.object(model, "complete", complete),
                patch.object(tools, "execute", execute),
            ):
                result = await run(
                    Query(question="topic", lang="en"),
                    model,
                    tools,
                    "",
                    Limits(tool_calls=limit),
                    retry_web=True,
                )
            self.assertEqual(result.state, expected)
            self.assertEqual(result.tool_calls, 2 if limit == 10 else 1)
            if limit == 10:
                self.assertNotEqual(
                    execute.call_args_list[0].args[0].arguments,
                    execute.call_args_list[1].args[0].arguments,
                )
                self.assertEqual(result.cost, Decimal("0.02"))


class IntentBoundaryTests(unittest.TestCase):
    def test_edit_intent_is_explicit_through_public_api(self) -> None:
        import base64
        import tempfile
        import os
        from pathlib import Path
        from fastapi.testclient import TestClient
        from services.orchestrator.chat_api import app

        encoded = base64.b64encode(
            Path("tests/cassettes/vision/shapes.png").read_bytes()
        ).decode()
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch(
                "services.orchestrator.model.GatewayModel.post", AsyncMock()
            ) as upstream,
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Pourrais-tu modifier cette image ?",
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "data:image/png;base64," + encoded
                                    },
                                },
                            ],
                        }
                    ]
                },
            )
        self.assertEqual(response.status_code, 200)
        answer = response.json()["choices"][0]["message"]["content"]
        self.assertIn("pas prise en charge", answer)
        self.assertIn("générer", answer)
        upstream.assert_not_called()

    def test_seed_validation_at_chat_boundary(self) -> None:
        from pydantic import ValidationError

        message = ChatMessage(role="user", content="Generate an image of a cube")
        self.assertEqual(ChatRequest(messages=[message], seed=123).seed, 123)
        for seed in (-1, 2**32, True, "123"):
            with self.assertRaises(ValidationError):
                ChatRequest.model_validate({"messages": [message], "seed": seed})

    def test_followup_stream_preserves_existing_source_link(self) -> None:
        import json
        import os
        import tempfile
        from fastapi.testclient import TestClient
        from services.orchestrator.chat_api import app

        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=100,
            ),
        )
        link = (
            "[Source]("
            + atlas_ports().url("adapter", "localhost")
            + "/sources/"
            + "a" * 64
            + ")"
        )
        answer = "Die Pflanzen nutzen Licht. " + link

        async def complete(messages: object, timeout: float) -> Turn:
            assert model.sink
            for piece in (
                "Die Pflanzen nutzen Licht. [Source]("
                + atlas_ports().url("adapter", "localhost")
                + "/sources/",
                "a" * 64,
                ")",
            ):
                await model.sink({"delta": {"content": piece}})
            return Turn(answer, usage=Reservation(10, Decimal(0)))

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(model, "complete", side_effect=complete),
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "stream": True,
                    "messages": [
                        {
                            "role": "assistant",
                            "content": "Les plantes utilisent la lumière. " + link,
                        },
                        {
                            "role": "user",
                            "content": "Traduis ta réponse précédente en allemand.",
                        },
                    ],
                },
            )
        events = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        self.assertFalse(any("error" in event for event in events))
        text = "".join(
            event["choices"][0]["delta"].get("content", "") for event in events
        )
        self.assertEqual(text, answer)
        self.assertEqual(model.tools, [])
