"""Model routing of plain chat turns (ATLAS_MODEL_ROUTER) and the gates that read it."""

import asyncio
import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from services.orchestrator import request_router
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Result
from services.orchestrator.request_router import DECISION_KEY, Decision

ARTIFACT_LINK = (
    "/api/atlas/artifacts/" + "a" * 32 + "/versions/1/open"
)  # an app delivered earlier


def request(
    text: str, history: list[dict[str, str]] | None = None, **fields: object
) -> ChatRequest:
    return ChatRequest.model_validate(
        {
            "messages": [*(history or []), {"role": "user", "content": text}],
            "ui_locale": "en",
            "lang": "en",
            **fields,
        }
    )


def routed(request_: ChatRequest, **decision: Any) -> ChatRequest:
    request_.__dict__[DECISION_KEY] = Decision.model_validate(decision)
    return request_


def reply(text: str, cost: str = "0.0002") -> dict[str, object]:
    return {"text": text, "calls": [], "cost_eur": cost}


@contextmanager
def router_on() -> Iterator[None]:
    with patch.dict(os.environ, {"ATLAS_MODEL_ROUTER": "1"}):
        yield


class ParseTests(unittest.TestCase):
    def test_valid_and_tolerant_outputs(self) -> None:
        self.assertEqual(
            request_router.parse('{"kind": "drawing", "file_format": null}'),
            Decision(kind="drawing"),
        )
        fenced = '```json\n{"kind": "file", "file_format": "Excel"}\n```'
        self.assertEqual(
            request_router.parse(fenced), Decision(kind="file", file_format="xlsx")
        )
        prose = 'Here: {"kind": "APP", "follow_up": true} done'
        self.assertEqual(
            request_router.parse(prose), Decision(kind="app", follow_up=True)
        )
        # A format outside a file request is dropped, an unknown one is unspecified.
        self.assertIsNone(
            request_router.parse('{"kind": "answer", "file_format": "pdf"}').file_format
        )
        self.assertIsNone(
            request_router.parse('{"kind": "file", "file_format": "odt"}').file_format
        )

    def test_invalid_outputs_raise(self) -> None:
        for text in ("", "no json here", '{"kind": "poem"}', '{"format": "pdf"}'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    request_router.parse(text)

    def test_prompt_carries_turns_as_data_and_marks_deliveries(self) -> None:
        messages = request_router.messages(
            "plus grand",
            [
                {"role": "user", "content": "fais un jeu de dames"},
                {"role": "assistant", "content": "Voici le jeu: " + ARTIFACT_LINK},
            ],
        )
        self.assertEqual(messages[0]["role"], "system")
        data = json.loads(messages[1]["content"])
        self.assertEqual(data["last_message"], "plus grand")
        self.assertIn(
            "[delivered: an interactive app", data["earlier_turns"][1]["text"]
        )

    def test_profile_comes_from_the_routing_table(self) -> None:
        import yaml

        routing = yaml.safe_load(
            Path("services/model-gateway/routing.yaml").read_text()
        )
        self.assertEqual(routing["public_profiles"][request_router.profile()], "fast")
        payload = request_router.payload("hello", [])
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["tools"], [])
        self.assertLessEqual(payload["timeout"], 10)


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_flag_off_returns_none_without_a_call(self) -> None:
        gateway = AsyncMock(return_value=reply('{"kind": "drawing"}'))
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("services.orchestrator.request_router._gateway", gateway),
        ):
            os.environ.pop("ATLAS_MODEL_ROUTER", None)
            req = request("Draw me a sheep")
            self.assertIsNone(await request_router.route(req))
        gateway.assert_not_awaited()
        self.assertNotIn(DECISION_KEY, req.__dict__)

    async def test_valid_decision_is_stored_once_and_logged(self) -> None:
        gateway = AsyncMock(
            return_value=reply('{"kind": "drawing", "follow_up": false}')
        )
        req = request("Draw me a sheep")
        with (
            router_on(),
            patch("services.orchestrator.request_router._gateway", gateway),
            self.assertLogs("services.orchestrator.request_router", "INFO") as logs,
        ):
            self.assertEqual(await request_router.route(req), Decision(kind="drawing"))
            self.assertEqual(request_router.decision(req), Decision(kind="drawing"))
            await request_router.route(req)
        gateway.assert_awaited_once()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("request_router kind=drawing", logs.output[0])
        self.assertIn("fallback=none", logs.output[0])
        # Never the user's text.
        self.assertNotIn("sheep", logs.output[0])
        self.assertEqual(req.__dict__[request_router.COST_KEY], Decimal("0.0002"))

    async def test_invalid_json_falls_back(self) -> None:
        req = request("Draw me a sheep")
        with (
            router_on(),
            patch(
                "services.orchestrator.request_router._gateway",
                AsyncMock(return_value=reply("drawing, I think")),
            ),
            self.assertLogs("services.orchestrator.request_router", "INFO") as logs,
        ):
            self.assertIsNone(await request_router.route(req))
        self.assertIn("fallback=router_invalid_json", logs.output[0])
        self.assertIsNone(request_router.decision(req))

    async def test_timeout_falls_back_within_the_bound(self) -> None:
        async def slow(payload: dict[str, Any], seconds: float) -> dict[str, Any]:
            await asyncio.sleep(5)
            return reply('{"kind": "app"}')

        req = request("Make me a game")
        with (
            router_on(),
            patch.dict(os.environ, {"ATLAS_MODEL_ROUTER_TIMEOUT": "0.5"}),
            patch("services.orchestrator.request_router._gateway", slow),
            self.assertLogs("services.orchestrator.request_router", "INFO") as logs,
        ):
            started = asyncio.get_running_loop().time()
            self.assertIsNone(await request_router.route(req))
            self.assertLess(asyncio.get_running_loop().time() - started, 2)
        self.assertIn("fallback=timeout", logs.output[0])

    async def test_gateway_error_and_tool_call_fall_back(self) -> None:
        for gateway in (
            AsyncMock(side_effect=RuntimeError("gateway_unavailable")),
            AsyncMock(return_value={"text": "", "calls": [{"name": "x"}]}),
        ):
            with self.subTest(gateway=gateway):
                with (
                    router_on(),
                    patch("services.orchestrator.request_router._gateway", gateway),
                ):
                    self.assertIsNone(await request_router.route(request("hello")))

    async def test_turns_that_keep_their_handling_are_not_routed(self) -> None:
        gateway = AsyncMock(return_value=reply('{"kind": "answer"}'))
        cases = [
            request("### Task:\nSuggest 3 follow-up questions"),
            request(
                "Summarise this",
                documents=[
                    {"filename": "a.pdf", "file_data": "data:application/pdf;base64,AA"}
                ],
            ),
            request(
                "x",
                project_id="123e4567-e89b-12d3-a456-426614174000",
                conversation_id="123e4567-e89b-12d3-a456-426614174001",
            ),
        ]
        skipped = request("hello")
        skipped.__dict__[request_router.SKIP_KEY] = True
        cases.append(skipped)
        with (
            router_on(),
            patch("services.orchestrator.request_router._gateway", gateway),
        ):
            for req in cases:
                self.assertIsNone(await request_router.route(req))
        gateway.assert_not_awaited()

    async def test_decision_is_ignored_when_the_flag_is_off(self) -> None:
        req = routed(request("Write a poem"), kind="file", file_format="pdf")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATLAS_MODEL_ROUTER", None)
            self.assertIsNone(request_router.decision(req))
            self.assertEqual(req.timeout_seconds, 120.0)


class SchemaGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ, {"ATLAS_MODEL_ROUTER": "1", "ATLAS_ARTIFACTS": "1"}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.images = patch(
            "services.orchestrator.image_tool.images_enabled", return_value=False
        )
        self.images.start()
        self.addCleanup(self.images.stop)

    def owned(self, req: ChatRequest) -> ChatRequest:
        req.__dict__["artifact_owner"] = "owner@example.test"
        return req

    def test_app_gets_the_app_deadline(self) -> None:
        req = self.owned(routed(request("un petit jeu de calcul"), kind="app"))
        self.assertTrue(req.app_turn)
        self.assertEqual(req.timeout_seconds, 300.0)

    def test_drawing_with_images_off_is_an_app_turn(self) -> None:
        req = self.owned(
            routed(request("Je voudrais que tu me dessines un mouton"), kind="drawing")
        )
        self.assertTrue(req.app_turn)
        self.assertEqual(req.timeout_seconds, 300.0)

    def test_drawing_with_images_on_is_not_an_app_turn(self) -> None:
        with patch(
            "services.orchestrator.image_tool.images_enabled", return_value=True
        ):
            req = self.owned(routed(request("Draw me a sheep"), kind="drawing"))
            self.assertFalse(req.app_turn)
            explicit = self.owned(
                routed(request("Draw me a sheep in SVG"), kind="drawing")
            )
            self.assertTrue(explicit.app_turn)

    def test_answer_overrides_the_lexical_app_test(self) -> None:
        # The lexical test reads "make" + "tool" as an app; the model says answer.
        req = self.owned(
            routed(request("make a list of tools for gardening"), kind="answer")
        )
        self.assertFalse(req.app_turn)
        self.assertEqual(req.timeout_seconds, 120.0)

    def test_file_gets_the_file_deadline(self) -> None:
        req = routed(
            request("make me something to track my expenses"),
            kind="file",
            file_format="xlsx",
        )
        self.assertEqual(req.timeout_seconds, 300.0)


class WorkGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_work_kinds_escalate_and_others_stay(self) -> None:
        from services.orchestrator.chat_work import should_escalate

        with (
            router_on(),
            patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}),
            patch("services.orchestrator.chat_work.work_gate_open", return_value=True),
        ):
            for kind in ("file", "run", "remote"):
                with self.subTest(kind=kind):
                    self.assertTrue(
                        should_escalate(routed(request("do the thing"), kind=kind))
                    )
            for kind in ("answer", "research", "drawing", "app", "other"):
                with self.subTest(kind=kind):
                    # "Write ... in Python and run it" is Work for the lexical test.
                    self.assertFalse(
                        should_escalate(
                            routed(
                                request("Write a game in Python and run it"), kind=kind
                            )
                        )
                    )

    async def test_file_reaches_process_work_with_its_format(self) -> None:
        from services.orchestrator.chat_work import escalate_to_work

        req = routed(
            request("j'ai besoin d'un truc pour suivre mes dépenses"),
            kind="file",
            file_format="xlsx",
        )
        req.__dict__["native_chat_id"] = "123e4567-e89b-12d3-a456-426614174010"
        req.__dict__["artifact_owner"] = "user@example.com"
        seen: list[ChatRequest] = []

        async def fake_process_work(
            scoped: ChatRequest, item: Interaction, **kwargs: object
        ) -> None:
            seen.append(scoped)
            item.reponse, item.state = "done", "done"

        identifier = "123e4567-e89b-12d3-a456-42661417400"
        with (
            router_on(),
            patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}),
            patch("services.orchestrator.chat_work.work_gate_open", return_value=True),
            patch(
                "services.orchestrator.chat_work._identity",
                new=AsyncMock(
                    return_value={"memberships": [{"org_id": identifier + "0"}]}
                ),
            ),
            patch(
                "services.orchestrator.chat_work._ensure_scratch_project",
                new=AsyncMock(return_value=identifier + "2"),
            ),
            patch(
                "services.orchestrator.chat_work._find_binding",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "services.orchestrator.chat_work._create_conversation",
                new=AsyncMock(return_value=(identifier + "3", 0)),
            ),
            patch(
                "services.orchestrator.chat_work._ensure_work_session",
                new=AsyncMock(return_value=identifier + "4"),
            ),
            patch("services.orchestrator.chat_work._bind_conversation", AsyncMock()),
            patch("services.orchestrator.code_mode.process_work", fake_process_work),
        ):
            self.assertTrue(await escalate_to_work(req, Interaction()))
            # The decision travels with the Work request: .xlsx is the named format.
            self.assertEqual(
                request_router.decision(seen[0]), req.__dict__[DECISION_KEY]
            )
            routed_decision = request_router.decision(seen[0])
            assert routed_decision is not None
            self.assertEqual(routed_decision.office_format, "xlsx")

    async def test_answer_stays_in_the_chat_without_any_call(self) -> None:
        from services.orchestrator.chat_work import escalate_to_work

        req = routed(request("Écris un script Python et lance-le"), kind="answer")
        req.__dict__["native_chat_id"] = "123e4567-e89b-12d3-a456-426614174010"
        req.__dict__["artifact_owner"] = "user@example.com"
        identity = AsyncMock(side_effect=AssertionError("no Work setup"))
        with (
            router_on(),
            patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}),
            patch("services.orchestrator.chat_work.work_gate_open", return_value=True),
            patch("services.orchestrator.chat_work._identity", identity),
        ):
            self.assertFalse(await escalate_to_work(req, Interaction()))
        identity.assert_not_awaited()


class PipelineGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(
            os.environ,
            {
                "ATLAS_MODEL_ROUTER": "1",
                "ATLAS_ARTIFACTS": "1",
                "ATLAS_DATA_DIR": self.directory.name,
                "ATLAS_WEB_CACHE": str(Path(self.directory.name) / "web.sqlite"),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def model(self) -> Mock:
        model = Mock(generation_ms=1.0, provider_retries=0)
        model.input_tokens, model.output_tokens = 10, 5
        model.observations = []
        model.tools = []
        model.prefix = []
        model.sink = None
        model.spent = Decimal(0)
        model.has_attachments = False
        model.produces_files = False
        model.required_file_tool = None
        model.max_tokens = 6000
        model.reasoning_ceiling = None
        return model

    async def process(
        self, req: ChatRequest, *, images: bool = False, owner: str = "o@example.test"
    ) -> tuple[AsyncMock, AsyncMock, AsyncMock, Interaction]:
        req.__dict__["daily_user"] = "user@example.test"
        req.__dict__["artifact_owner"] = owner
        item = Interaction()
        run = AsyncMock(
            return_value=Result("Here it is.", "done", cost=Decimal("0.01"))
        )
        with (
            patch(
                "services.orchestrator.chat_pipeline.GatewayModel.connect",
                new=AsyncMock(return_value=self.model()),
            ),
            patch("services.orchestrator.chat_pipeline.run", new=run),
            patch(
                "services.orchestrator.chat_pipeline.images_enabled",
                return_value=images,
            ),
            patch(
                "services.orchestrator.image_tool.images_enabled", return_value=images
            ),
            patch(
                "services.orchestrator.chat_pipeline.process_image", AsyncMock()
            ) as image,
            patch(
                "services.orchestrator.chat_pipeline._process_simple_drawing",
                AsyncMock(),
            ) as inline_svg,
            patch(
                "services.orchestrator.chat_pipeline.process_app_turn", AsyncMock()
            ) as app_turn,
        ):
            self.app_turn = app_turn
            from services.orchestrator.chat_pipeline import _process

            await _process(req, item)
        return run, image, inline_svg, item

    async def test_drawing_with_images_off_goes_to_the_svg_app_panel(self) -> None:
        from services.orchestrator.image_tool import unavailable_reply

        for text in (
            "Je voudrais que tu me dessines un mouton. Un mouton à 5 pattes.",
            "Dessine-moi un mouton",
            "Could you sketch a lighthouse at dusk?",
        ):
            with self.subTest(text=text):
                run, image, inline_svg, item = await self.process(
                    routed(request(text), kind="drawing")
                )
                run.assert_not_awaited()
                image.assert_not_awaited()
                inline_svg.assert_not_awaited()
                self.assertEqual(item.task_type, "artifact")
                self.app_turn.assert_awaited_once()
                self.assertEqual(self.app_turn.call_args.kwargs["kind"], "drawing")
                self.assertEqual(self.app_turn.call_args.kwargs["intent"], "create")
                self.assertNotEqual(item.reponse, unavailable_reply("fr"))
                self.assertNotEqual(item.reponse, unavailable_reply("en"))

    async def test_drawing_without_the_panel_is_drawn_inline(self) -> None:
        run, image, inline_svg, item = await self.process(
            routed(request("Je voudrais que tu me dessines un mouton"), kind="drawing"),
            owner="",
        )
        inline_svg.assert_awaited_once()
        run.assert_not_awaited()
        image.assert_not_awaited()

    async def test_drawing_with_images_on_generates_an_image(self) -> None:
        run, image, inline_svg, _ = await self.process(
            routed(request("J'aimerais bien un dessin de mouton"), kind="drawing"),
            images=True,
        )
        image.assert_awaited_once()
        run.assert_not_awaited()
        inline_svg.assert_not_awaited()

    async def test_lexical_fallback_is_unchanged(self) -> None:
        from services.orchestrator.image_tool import unavailable_reply

        # No decision (router off or failed): today's lexical routes, both of the
        # failures the router removes included.
        _, image, inline_svg, item = await self.process(
            request("Dessine-moi un mouton", lang="fr")
        )
        self.assertEqual(item.reponse, unavailable_reply("fr"))
        image.assert_not_awaited()
        inline_svg.assert_not_awaited()
        run, image, inline_svg, item = await self.process(
            request("Je voudrais que tu me dessines un mouton", lang="fr")
        )
        # A plain chat answer (text or ASCII art), not a drawing.
        run.assert_awaited_once()
        self.assertNotEqual(item.task_type, "artifact")
        image.assert_not_awaited()
        inline_svg.assert_not_awaited()

    async def test_app_creates_and_app_follow_up_modifies(self) -> None:
        run, _, _, item = await self.process(
            routed(request("un truc pour s'entraîner aux tables de 7"), kind="app")
        )
        self.assertEqual(item.task_type, "artifact")
        run.assert_not_awaited()
        self.assertEqual(self.app_turn.call_args.kwargs["intent"], "create")
        self.assertEqual(self.app_turn.call_args.kwargs["kind"], "app")
        history = [
            {"role": "user", "content": "un jeu de dames"},
            {"role": "assistant", "content": "Voici le jeu: " + ARTIFACT_LINK},
        ]
        current = Mock(
            artifact={"artifact_id": "a" * 32, "version": 1},
            message={"role": "user", "content": "{}"},
        )
        with patch(
            "services.orchestrator.chat_pipeline.current_artifact_message",
            return_value=current,
        ):
            run, _, _, item = await self.process(
                routed(request("plus grand", history), kind="app", follow_up=True)
            )
        self.assertEqual(item.task_type, "artifact")
        run.assert_not_awaited()
        # The service resolves the artifact to change; the model never names it.
        self.assertEqual(self.app_turn.call_args.kwargs["intent"], "modify")
        self.assertIs(self.app_turn.call_args.kwargs["current"], current)

    async def test_answer_stays_a_chat_answer(self) -> None:
        # A request to create a small calculator is an app for the lexical test.
        run, image, inline_svg, item = await self.process(
            routed(request("Crée une petite calculatrice"), kind="answer")
        )
        run.assert_awaited_once()
        self.assertNotEqual(item.task_type, "artifact")
        self.assertNotIn(
            "create_artifact",
            [tool["function"]["name"] for tool in run.call_args.args[1].tools],
        )

    async def test_file_kind_sets_the_file_flags_and_format(self) -> None:
        run, _, _, _ = await self.process(
            routed(
                request("un tableau pour suivre mes dépenses à télécharger"),
                kind="file",
                file_format="xlsx",
            )
        )
        self.assertEqual(run.call_args.args[4].cost, Decimal("0.30"))
        self.assertTrue(run.call_args.args[4].produces_files)


class CapabilityStatementTests(unittest.TestCase):
    def test_flag_off_keeps_the_existing_text(self) -> None:
        from services.orchestrator.image_tool import guest_capability_instruction

        with patch.dict(os.environ, {"ATLAS_STABLE": "1"}):
            os.environ.pop("ATLAS_MODEL_ROUTER", None)
            self.assertIn("Do not offer any of them", guest_capability_instruction())

    def test_flag_on_states_what_is_delivered_never_a_denial(self) -> None:
        from services.orchestrator.image_tool import guest_capability_instruction

        for work in (True, False):
            with (
                self.subTest(work=work),
                router_on(),
                patch.dict(os.environ, {"ATLAS_STABLE": "1", "ATLAS_CHAT_WORK": "1"}),
                patch(
                    "services.orchestrator.image_tool.images_enabled",
                    return_value=False,
                ),
                patch(
                    "services.orchestrator.chat_work.work_gate_open", return_value=work
                ),
            ):
                text = guest_capability_instruction()
                self.assertIn("drawn as SVG", text)
                self.assertNotIn("Do not offer", text)
                self.assertNotIn("unavailable here", text)
                self.assertEqual("Work mode" in text, work)


class DeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_extend_moves_the_current_deadline(self) -> None:
        from services.orchestrator.deadline import extend, request_deadline

        async with request_deadline(0.2):
            extend(1.0)
            await asyncio.sleep(0.4)


if __name__ == "__main__":
    unittest.main()
