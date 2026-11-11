import json
import os
import tempfile
import time
import unittest
from collections.abc import Callable, Coroutine
from typing import Any, cast
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from services.orchestrator import artifact_store
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.daily_budget import DailyLedger
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Result


HTML = "<html><body><button>Run</button></body></html>"


class ChatArtifactTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(
            os.environ,
            {
                "ATLAS_ARTIFACTS": "1",
                "ATLAS_DATA_DIR": self.directory.name,
                "ATLAS_WEB_CACHE": str(Path(self.directory.name) / "web.sqlite"),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def request(
        self, text: str, history: list[dict[str, str]] | None = None
    ) -> ChatRequest:
        return ChatRequest.model_validate(
            {
                "messages": [
                    *(history or []),
                    {"role": "user", "content": text},
                ]
            }
        )

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
        self,
        request: ChatRequest,
        result: Result | None = None,
        *,
        run: AsyncMock | Callable[..., Coroutine[Any, Any, Result]] | None = None,
        artifact_owner: str = "owner@example.test",
    ) -> tuple[AsyncMock, Interaction]:
        model = self.model()
        item = Interaction()
        request.__dict__["daily_user"] = "user@example.test"
        request.__dict__["artifact_owner"] = artifact_owner
        with (
            patch(
                "services.orchestrator.chat_pipeline.GatewayModel.connect",
                new=AsyncMock(return_value=model),
            ),
            patch(
                "services.orchestrator.chat_pipeline.run",
                new=run if run is not None else AsyncMock(return_value=result),
            ) as run,
            patch(
                "services.orchestrator.chat_pipeline.process_app_turn", AsyncMock()
            ) as app_turn,
        ):
            self.app_turn = app_turn
            from services.orchestrator.chat_pipeline import _process

            await _process(request, item)
        return cast(AsyncMock, run), item

    async def test_create_goes_to_the_app_turn_without_the_tool_loop(self) -> None:
        run, item = await self.process(
            self.request("Create a small web app."),
            Result("Here it is.", "done", cost=Decimal("0.01")),
        )
        # The file is written in the answer of the app turn, never as the
        # arguments of a tool call in the general loop.
        run.assert_not_awaited()
        self.app_turn.assert_awaited_once()
        self.assertEqual(self.app_turn.call_args.kwargs["intent"], "create")
        self.assertEqual(self.app_turn.call_args.kwargs["kind"], "app")
        self.assertIsNone(self.app_turn.call_args.kwargs["current"])
        self.assertEqual(item.task_type, "artifact")

    async def test_svg_request_is_a_drawing_turn(self) -> None:
        run, item = await self.process(
            self.request("Dessine un chat en SVG."),
            Result("Here it is.", "done", cost=Decimal("0.01")),
        )
        run.assert_not_awaited()
        self.assertEqual(self.app_turn.call_args.kwargs["kind"], "drawing")

    async def test_feature_off_is_byte_identical(self) -> None:
        os.environ.pop("ATLAS_ARTIFACTS")
        run, item = await self.process(
            self.request("Create a small web app."),
            Result("Here it is.", "done", cost=Decimal("0.01")),
        )
        model = run.call_args.args[1]
        self.assertNotIn(
            "create_artifact", [tool["function"]["name"] for tool in model.tools]
        )
        self.assertNotIn(
            Path("prompts/artifact.txt").read_text(), run.call_args.args[3]
        )
        self.assertEqual(model.max_tokens, 6000)
        self.assertIsNone(model.reasoning_ceiling)
        self.assertEqual(run.call_args.args[4].cost, Decimal("0.10"))
        self.assertEqual(item.task_type, "text")

    def history(self) -> tuple[str, list[dict[str, str]]]:
        created = artifact_store.create(
            "user@example.test", "Runner", "runner.html", HTML.encode(), "request-1"
        )
        artifact_id = str(created["id"])
        return artifact_id, [
            {
                "role": "assistant",
                "content": f"/api/atlas/artifacts/{artifact_id}/versions/1/open",
            }
        ]

    async def test_follow_up_passes_the_current_artifact(self) -> None:
        artifact_id, history = self.history()
        run, item = await self.process(
            self.request("Change the app.", history),
            Result("Changed.", "done", cost=Decimal("0.01")),
            artifact_owner="user@example.test",
        )
        run.assert_not_awaited()
        self.assertEqual(item.task_type, "artifact")
        self.assertEqual(self.app_turn.call_args.kwargs["intent"], "modify")
        current = self.app_turn.call_args.kwargs["current"]
        self.assertEqual(current.artifact["artifact_id"], artifact_id)
        self.assertEqual(current.artifact["content"], HTML)
        self.assertNotIn(artifact_id, str(current.message["content"]))

    async def test_stable_mode_keeps_the_app_turn(self) -> None:
        os.environ["ATLAS_STABLE"] = "1"
        run, _ = await self.process(
            self.request("Create a small web app."),
            Result("Here it is.", "done", cost=Decimal("0.01")),
        )
        run.assert_not_awaited()
        self.app_turn.assert_awaited_once()

    async def test_dessine_changes_the_current_artifact(self) -> None:
        for images in (True, False):
            with self.subTest(images=images):
                artifact_id, history = self.history()
                with patch(
                    "services.orchestrator.chat_pipeline.images_enabled",
                    return_value=images,
                ):
                    run, _ = await self.process(
                        self.request("Dessine le bouton en rouge.", history),
                        Result("Changed.", "done", cost=Decimal("0.01")),
                        artifact_owner="user@example.test",
                    )
                run.assert_not_awaited()
                self.assertEqual(self.app_turn.call_args.kwargs["intent"], "modify")
                current = self.app_turn.call_args.kwargs["current"]
                self.assertEqual(current.artifact["artifact_id"], artifact_id)

    async def test_daily_reservation_remains_text_ceiling(self) -> None:
        from decimal import Decimal

        ledger = DailyLedger()
        request = self.request("Create a small web app.")
        request.__dict__["daily_user"] = "user@example.test"
        reservation = ledger.reserve_cost(
            request.__dict__["daily_user"],
            Decimal("0.30" if request.has_attachments else "0.10"),
            request.lang,
        )
        self.assertEqual(reservation.amount, Decimal("0.10"))

    async def test_no_artifact_owner_disables_tool(self) -> None:
        run, item = await self.process(
            self.request("Create a small web app."),
            Result("Here it is.", "done", cost=Decimal("0.01")),
            artifact_owner="",
        )
        model = run.call_args.args[1]
        self.assertNotIn(
            "create_artifact", [tool["function"]["name"] for tool in model.tools]
        )
        self.assertIsNone(model.required_file_tool)
        self.app_turn.assert_not_awaited()
        self.assertEqual(item.task_type, "text")
        self.assertEqual(run.call_args.args[4].cost, Decimal("0.10"))

    async def test_streaming_preserves_artifact_links(self) -> None:
        from services.orchestrator.chat_stream import response
        from services.orchestrator.stream_client import sink_context

        created = artifact_store.create(
            "user@example.test", "Runner", "runner.html", HTML.encode(), "request-1"
        )
        artifact_id = str(created["id"])
        item = Interaction()
        item.artifacts = [
            {
                "id": artifact_id,
                "version": 1,
                "bytes": len(HTML.encode()),
                "sha256": "0" * 64,
            }
        ]
        item.reponse = (
            f"Open /api/atlas/artifacts/{artifact_id}/versions/1/open · "
            f"Download /api/atlas/artifacts/{artifact_id}/versions/1/download"
        )
        item.state = "done"

        async def process(request: ChatRequest, interaction: Interaction) -> None:
            sink = sink_context.get()
            assert sink is not None
            await sink({"delta": {"content": interaction.reponse}})

        request = self.request("Create a small web app.")
        request.atlas_ui = True
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
        ):
            events = [
                json.loads(str(event)[6:])
                async for event in response(
                    request, item, time.monotonic(), process
                ).body_iterator
                if str(event).startswith("data: {")
            ]
        text = "".join(
            event.get("choices", [{}])[0].get("delta", {}).get("content", "")
            for event in events
        )
        self.assertIn(f"/api/atlas/artifacts/{artifact_id}/versions/1/open", text)
        self.assertIn(f"/api/atlas/artifacts/{artifact_id}/versions/1/download", text)


if __name__ == "__main__":
    unittest.main()
