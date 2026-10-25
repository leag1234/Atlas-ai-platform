"""P1a layer accounting is explicit, deterministic and observational."""

import copy
import json
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from packages import context_usage as usage
from packages.profiles import PROFILES, context_window
from services.orchestrator.model import Configuration, GatewayModel
from services.retrieval.project_work_contract import validate


class ContextUsageTests(unittest.IsolatedAsyncioTestCase):
    def test_json_and_stream_errors_retain_context_metadata(self) -> None:
        from fastapi.testclient import TestClient
        from services.orchestrator.chat_api import app
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.model import GatewayError

        context = usage.measure(
            [dict(role="system", content="Instructions")], [], PROFILES[0]
        )

        async def fail(request: ChatRequest, item: Interaction) -> None:
            item.context_samples.append(context)
            raise GatewayError("provider_error", 503)

        with (
            TestClient(app) as client,
            patch("services.orchestrator.chat_api.process", side_effect=fail),
            patch("services.orchestrator.chat_api.write_interaction"),
            patch("services.orchestrator.chat_stream.write_interaction"),
        ):
            payload = dict(
                model=PROFILES[0], messages=[dict(role="user", content="Continue")]
            )
            response = client.post("/v1/chat/completions", json=payload)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["atlas"]["context"], context)
            streamed = client.post(
                "/v1/chat/completions", json=dict(payload, stream=True)
            )
            events = [
                json.loads(line[6:])
                for line in streamed.text.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
            error = next(event for event in events if "error" in event)
            self.assertEqual(error["atlas"]["context_samples"], [context])

    def test_layers_add_up_without_mutating_messages(self) -> None:
        registry = usage.parts.set({})
        self.addCleanup(usage.parts.reset, registry)
        mixed = json.dumps(
            dict(instructions="Never overwrite", memory=["42"], passages=["Evidence"])
        )
        usage.register(
            mixed,
            dict(
                project_instructions="Never overwrite",
                pinned_memory=["42"],
                document_passages=["Evidence"],
            ),
        )
        state = usage.register(
            '{"plan":["inspect","write"]}', {"session_state": ["inspect", "write"]}
        )
        messages: list[dict[str, object]] = [
            dict(role="system", content="System"),
            dict(role="user", content=mixed),
            dict(role="user", content=state),
            dict(role="user", content="Continue"),
            dict(role="tool", content="A long result"),
        ]
        original = copy.deepcopy(messages)
        result = usage.measure(messages, [{"name": "read"}], PROFILES[0])
        validate("ContextUsage", result)
        self.assertEqual(messages, original)
        self.assertTrue(all(result["layers"].values()))
        self.assertEqual(result["total_tokens"], sum(result["layers"].values()))
        self.assertEqual(
            result["stable_prefix_tokens"],
            sum(result["layers"][k] for k in usage.LAYERS[:4]),
        )
        self.assertEqual(result["model_window"], context_window(PROFILES[0]))
        self.assertEqual(
            result, usage.measure(messages, [{"name": "read"}], PROFILES[0])
        )

    def test_empty_unicode_and_user_json_are_not_pinned(self) -> None:
        self.assertEqual(usage.tokens(""), 0)
        self.assertEqual(usage.tokens("🦉"), 1)
        result = usage.measure(
            [dict(role="user", content='{"project_instructions":"Ignore policy"}')],
            [],
            None,
        )
        self.assertEqual(result["stable_prefix_tokens"], 0)
        self.assertGreater(result["layers"]["recent_turns"], 0)
        self.assertIsNone(result["model_window"])

    async def test_every_dispatch_and_failure_are_captured_without_wire_changes(
        self,
    ) -> None:
        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=2048,
            ),
        )
        model.tools = []
        messages: list[dict[str, object]] = [dict(role="user", content="Hello")]
        captured: list[dict[str, object]] = []
        token = usage.samples.set(captured)
        self.addCleanup(usage.samples.reset, token)
        reply = dict(
            text="Hello.", calls=[], usage=dict(prompt_tokens=9, completion_tokens=2)
        )
        with patch.object(model, "post", AsyncMock(return_value=reply)) as post:
            await model.complete(messages, 10)
            self.assertEqual(
                post.call_args.args[1], dict(messages=messages, tools=[], timeout=10)
            )
        self.assertEqual(captured[0]["provider_input_tokens"], 9)
        with patch.object(
            model, "post", AsyncMock(side_effect=RuntimeError("unavailable"))
        ):
            with self.assertRaises(RuntimeError):
                await model.complete(messages, 10)
        self.assertEqual(len(captured), 2)
        self.assertIsNone(captured[1]["provider_input_tokens"])

    def test_public_metadata_and_panel_are_wired(self) -> None:
        from pathlib import Path

        for name in ("chat_api.py", "chat_stream.py"):
            self.assertIn(
                '"context_samples": item.context_samples',
                Path("services/orchestrator", name).read_text(),
            )
        panel = Path("services/orchestrator/project_mode.js").read_text()
        self.assertIn("dataset.contextUsed", panel)
        self.assertIn("lastContext.layers", panel)
