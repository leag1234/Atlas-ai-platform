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
from services.orchestrator.chat_pipeline import ChatTools
from services.orchestrator.daily_budget import DailyLedger
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call, Result


HTML = "<html><body><button>Run</button></body></html>"
UPDATED_HTML = "<html><body><button>Changed</button></body></html>"


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
        ):
            from services.orchestrator.chat_pipeline import _process

            await _process(request, item)
        return cast(AsyncMock, run), item

    async def test_create_is_offered_forced_and_configured(self) -> None:
        events = []

        from services.orchestrator.stream_client import sink_context

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        token = sink_context.set(sink)
        try:
            run, item = await self.process(
                self.request("Create a small web app."),
                Result("Here it is.", "done", cost=Decimal("0.01")),
            )
        finally:
            sink_context.reset(token)
        model = run.call_args.args[1]
        self.assertIn(
            "create_artifact", [tool["function"]["name"] for tool in model.tools]
        )
        self.assertEqual(model.required_file_tool, "create_artifact")
        self.assertEqual(
            model.configure_quality.call_args.args[1],
            6000,
        )
        self.assertEqual(model.reasoning_ceiling, "none")
        self.assertEqual(run.call_args.args[4].cost, Decimal("0.30"))
        self.assertIn(Path("prompts/artifact.txt").read_text(), run.call_args.args[3])
        self.assertEqual(item.task_type, "artifact")
        self.assertEqual(
            [event for event in events if event.get("phase") == "tool_started"],
            [{"phase": "tool_started", "tool": "create_artifact", "urls": []}],
        )

    async def test_artifact_max_tokens_is_clamped_to_gateway_limit(self) -> None:
        with patch.dict(os.environ, {"ATLAS_ARTIFACT_MAX_TOKENS": "12000"}):
            run, _ = await self.process(
                self.request("Create a small web app."),
                Result("Here it is.", "done", cost=Decimal("0.01")),
            )
        model = run.call_args.args[1]
        self.assertEqual(model.configure_quality.call_args.args[1], 6000)

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

    async def test_successful_create_composes_footer_and_journal(self) -> None:
        arguments = {
            "title": "Runner",
            "filename": "runner.html",
            "content": HTML,
            "summary": "A simple app.",
        }
        result = Result("Here it is.", "done", cost=Decimal("0.01"))
        result.trace = [
            {
                "tool": "create_artifact",
                "arguments": json.dumps(arguments),
                "output": {"ok": True},
            }
        ]

        async def execute_artifact(
            query: object,
            model: object,
            tools: ChatTools,
            system: object,
            limits: object,
            **kwargs: object,
        ) -> Result:
            assert tools.artifacts is not None
            output = await tools.artifacts.execute(
                Call("call-1", "create_artifact", json.dumps(arguments)), 1
            )
            result.trace = [
                {
                    "tool": "create_artifact",
                    "arguments": json.dumps(arguments),
                    "output": output,
                }
            ]
            return result

        _, item = await self.process(
            self.request("Create a small web app."), run=execute_artifact
        )
        artifact = item.artifacts[0]
        self.assertEqual(set(artifact), {"id", "version", "bytes", "sha256"})
        self.assertEqual(artifact["bytes"], len(HTML.encode()))
        self.assertIn("/api/atlas/artifacts/", item.reponse)
        self.assertIn(
            f"[Télécharger runner.html](/api/atlas/artifacts/{artifact['id']}/versions/"
            f"{artifact['version']}/download)",
            item.reponse,
        )
        self.assertIn(
            f"[Ouvrir « Runner » (version {artifact['version']})]"
            f"(/api/atlas/artifacts/{artifact['id']}/versions/{artifact['version']}/open#",
            item.reponse,
        )
        self.assertIn("A simple app.", item.reponse)
        self.assertIn("artifact_ready", item.latence_ms)
        artifact_id = str(artifact["id"])
        self.assertEqual(
            artifact_store.read(artifact_id, "owner@example.test", 1).content,
            HTML.encode(),
        )
        with self.assertRaises(artifact_store.ArtifactError):
            artifact_store.read(artifact_id, "user@example.test", 1)
        self.assertEqual(
            {"generate_image", "create_artifact"},
            frozenset({"generate_image", "create_artifact"}),
        )

    async def test_modify_and_available_are_not_forced(self) -> None:
        created = artifact_store.create(
            "user@example.test", "Runner", "runner.html", HTML.encode(), "request-1"
        )
        artifact_id = str(created["id"])
        history = [
            {
                "role": "assistant",
                "content": f"/api/atlas/artifacts/{artifact_id}/versions/1/open",
            }
        ]
        run, item = await self.process(
            self.request("Change the app.", history),
            Result("Changed.", "done", cost=Decimal("0.01")),
        )
        model = run.call_args.args[1]
        self.assertIn(
            "create_artifact", [tool["function"]["name"] for tool in model.tools]
        )
        self.assertIsNone(model.required_file_tool)
        self.assertEqual(item.task_type, "artifact")

    async def test_stable_mode_keeps_artifact_tool(self) -> None:
        os.environ["ATLAS_STABLE"] = "1"
        run, _ = await self.process(
            self.request("Create a small web app."),
            Result("Here it is.", "done", cost=Decimal("0.01")),
        )
        model = run.call_args.args[1]
        self.assertIn(
            "create_artifact", [tool["function"]["name"] for tool in model.tools]
        )

    async def test_dessine_reaches_tool_loop_with_current_artifact(self) -> None:
        created = artifact_store.create(
            "user@example.test", "Runner", "runner.html", HTML.encode(), "request-1"
        )
        artifact_id = str(created["id"])
        history = [
            {
                "role": "assistant",
                "content": f"/api/atlas/artifacts/{artifact_id}/versions/1/open",
            }
        ]
        with patch(
            "services.orchestrator.chat_pipeline.images_enabled", return_value=True
        ):
            run, _ = await self.process(
                self.request("Dessine le bouton en rouge.", history),
                Result("Changed.", "done", cost=Decimal("0.01")),
                artifact_owner="user@example.test",
            )
        model = run.call_args.args[1]
        self.assertIn(
            "create_artifact", [tool["function"]["name"] for tool in model.tools]
        )
        history = run.call_args.kwargs["history"]
        self.assertTrue(
            all(
                isinstance(message, dict)
                and message.get("role") is not None
                and message.get("content") is not None
                for message in history
            )
        )
        self.assertEqual(history[-1]["role"], "system")
        self.assertIn("current_artifact", str(history[-1]["content"]))
        self.assertIn(artifact_id, str(history[-1]["content"]))

    async def test_dessine_reaches_tool_loop_with_current_artifact_images_disabled(
        self,
    ) -> None:
        created = artifact_store.create(
            "user@example.test", "Runner", "runner.html", HTML.encode(), "request-1"
        )
        artifact_id = str(created["id"])
        history = [
            {
                "role": "assistant",
                "content": f"/api/atlas/artifacts/{artifact_id}/versions/1/open",
            }
        ]
        with patch(
            "services.orchestrator.chat_pipeline.images_enabled", return_value=False
        ):
            run, _ = await self.process(
                self.request("Dessine le bouton en rouge.", history),
                Result("Changed.", "done", cost=Decimal("0.01")),
                artifact_owner="user@example.test",
            )
        model = run.call_args.args[1]
        self.assertIn(
            "create_artifact", [tool["function"]["name"] for tool in model.tools]
        )
        history = run.call_args.kwargs["history"]
        self.assertTrue(
            all(
                isinstance(message, dict)
                and message.get("role") is not None
                and message.get("content") is not None
                for message in history
            )
        )
        self.assertEqual(history[-1]["role"], "system")
        self.assertIn("current_artifact", str(history[-1]["content"]))
        self.assertIn(artifact_id, str(history[-1]["content"]))

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
