"""REQ-PROJ-04/05: replay a genuine provider Work session through the real loop."""

from __future__ import annotations

import copy
from dataclasses import asdict
from decimal import Decimal
import gzip
import json
import os
import sys
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch
from uuid import UUID

from packages import tool_results
from packages.context_usage import measure, parts, register
from services.orchestrator.code_mode import CodeModel, execute_turn
from services.orchestrator.code_tools import CodeTools
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.loop import Call, Limits, Message
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.result_references import ResultReferences
from services.orchestrator.work_tool_selection import WorkToolSelection

sys.path.insert(0, str(Path(__file__).resolve().parent))

from provider_recording import ExactHistory
from project_context_recording import BOUND

ARCHIVE = Path("tests/cassettes/project-context.json.gz")


class StoredReplay:
    """Recompute clearing and reads; recorded receipts independently check them."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.history = ExactHistory(rows)
        self.records: list[dict[str, Any]] = []
        self.opened: list[str] = []

    async def call(self, operation: str, data: Any, key: Any = None) -> Any:
        recorded = self.history.take(operation, data)
        if recorded is None:
            raise AssertionError("storage_request_mismatch:" + operation)
        if operation == "result-open":
            original = next(r for r in self.records if r["id"] == data["id"])
            if not original["cleared"]:
                raise AssertionError("reopen_must_read_a_cleared_result")
            result = dict(
                id=original["id"],
                tool=original["tool"],
                **tool_results.reopen(original["content"], data["range"]),
            )
            if result != recorded:
                raise AssertionError("reopened_content_changed")
            self.opened.append(original["id"])
            return result
        self.records.append(dict(data, cleared=False))
        changes = tool_results.clear(self.records, tool_results.keep_results())
        if changes != recorded["cleared"]:
            raise AssertionError("clearing_receipt_changed")
        for record in self.records:
            if record["id"] in {c["id"] for c in changes}:
                record["cleared"] = True
        return dict(id=data["id"], cleared=changes, events=[])


class RecordedTools(CodeTools):
    def __init__(
        self, rows: list[dict[str, Any]], selection: WorkToolSelection
    ) -> None:
        self.history = ExactHistory(rows)
        self.selection = selection
        self.test_evidence = []

    async def execute(self, call: Call, timeout: float) -> Message:
        result = self.history.take("tool", asdict(call))
        if not isinstance(result, dict):
            raise AssertionError("tool_request_mismatch:" + call.name)
        if call.name == "switch_tool_group":
            self.selection.switch(call.arguments)
        return result

    async def emit(self, kind: str, **data: Any) -> None:
        raise AssertionError("unexpected_replay_event:" + kind)


class ProjectContextRecordingTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_work_context_bound_and_lossless_reopen(self) -> None:
        path = Path(os.environ.get("ATLAS_CONTEXT_CASSETTE", str(ARCHIVE)))
        evidence = json.loads(gzip.decompress(path.read_bytes()))
        self.assertEqual(evidence["mode"], "live")
        self.assertEqual(evidence["provider"], "hetzner")
        self.assertEqual(evidence["http_status"], 200)
        self.assertEqual(evidence["bound"], BOUND)
        self.assertEqual(evidence["keep_results"], 4)
        self.assertEqual(evidence["result"]["state"], "done")
        self.assertGreaterEqual(evidence["result"]["tool_calls"], 25)
        self.assertLessEqual(evidence["result"]["tool_calls"], 40)
        self.assertTrue(evidence["result"]["text"].strip())
        self.assertLessEqual(Decimal(evidence["result"]["cost"]), Decimal("0.30"))
        completed = [
            event["event"]["result"]
            for row in evidence["provider_rows"]
            for event in row["response"]
            if "result" in event.get("event", {})
        ]
        ids = [result["provider_id"] for result in completed]
        self.assertGreaterEqual(len(ids), len(evidence["model_rows"]))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(ids))
        for row in evidence["model_rows"]:
            # The decoded wire response must be backed by an actual upstream result.
            self.assertTrue(
                any(
                    result["calls"] == row["response"]["calls"]
                    and result["text"] == row["response"]["text"]
                    for result in completed
                )
            )
        puts = [
            row["request"]
            for row in evidence["storage_rows"]
            if row["kind"] == "result-put"
        ]
        store = StoredReplay(evidence["storage_rows"])
        binding = puts[0]
        references = ResultReferences(
            store,
            binding["conversation_id"],
            binding["request_id"],
            binding["generation"],
        )
        selection = WorkToolSelection("documents")
        selection.reopen_enabled = True
        tools = RecordedTools(evidence["tool_rows"], selection)
        history = ExactHistory(evidence["model_rows"])
        model = CodeModel(
            "http://recorded.invalid",
            Configuration.model_validate(evidence["configuration"]),
        )
        request = ChatRequest.model_validate(evidence["turn"]["request"])
        registry = parts.set({})
        self.addCleanup(parts.reset, registry)
        for message in request.messages[:-1]:
            try:
                component = json.loads(message.text)
            except ValueError:
                continue
            if isinstance(component, dict) and set(component) == {
                "project_instructions",
                "memory",
                "passages",
                "coverage",
            }:
                register(
                    message.text,
                    dict(
                        project_instructions=component["project_instructions"],
                        pinned_memory=component["memory"],
                        document_passages=dict(
                            passages=component["passages"],
                            coverage=component["coverage"],
                        ),
                    ),
                )
        model.configure_quality(request.model, request.max_tokens)
        model.work_tools = selection.declarations
        model.work_cost = Decimal("0.30")
        model.work_per_call = Decimal(
            evidence["model_rows"][0]["request"]["budget_eur"]
        )
        compact: list[int] = []
        baseline: list[int] = []
        active: list[bool] = []
        originals = {
            tool_results.summary(p["id"], p["tool"], p["content"]): tool_results.full(
                p["id"], p["content"]
            )
            for p in puts
        }

        async def post(url: str, payload: Message, timeout: float) -> Message:
            self.assertTrue(url.endswith("/agent/complete"))
            replayed = history.take("complete", payload)
            self.assertIsNotNone(replayed, "model request differs from live recording")
            self.assertIsInstance(replayed, dict)
            assert isinstance(replayed, dict)
            messages = payload["messages"]
            assert isinstance(messages, list)
            compact.append(
                measure(messages, payload["tools"], request.model)["total_tokens"]
            )
            full = copy.deepcopy(payload["messages"])
            assert isinstance(full, list)
            cleared = False
            for message in full:
                if (
                    message.get("role") == "tool"
                    and message.get("content") in originals
                ):
                    message["content"] = originals[message["content"]]
                    cleared = True
            active.append(cleared)
            baseline.append(
                measure(full, payload["tools"], request.model)["total_tokens"]
            )
            return replayed

        limits = dict(evidence["turn"]["limits"])
        limits["cost"] = Decimal(limits["cost"])
        identifiers = [UUID(p["id"].split(":")[1]) for p in puts]
        with (
            patch.dict(
                os.environ,
                {
                    "ATLAS_PROJECT_MODE": "1",
                    "ATLAS_STABLE": "0",
                    "ATLAS_CONTEXT_KEEP_RESULTS": "4",
                },
            ),
            patch.object(GatewayModel, "post", staticmethod(post)),
            patch(
                "services.orchestrator.result_references.uuid4", side_effect=identifiers
            ),
        ):
            result = await execute_turn(
                request,
                model,
                tools,
                Limits(**limits),
                evidence["turn"]["workspace_context"],
                references,
            )
        self.assertEqual(result.state, "done")
        self.assertEqual(result.text, evidence["result"]["text"])
        self.assertEqual(result.tool_calls, evidence["result"]["tool_calls"])
        self.assertEqual(len(history.used), len(evidence["model_rows"]))
        self.assertEqual(len(store.history.used), len(evidence["storage_rows"]))
        self.assertEqual(len(tools.history.used), len(evidence["tool_rows"]))
        self.assertTrue(store.opened)
        self.assertGreater(sum(active), 10)
        for index, cleared in enumerate(active):
            if cleared:
                self.assertLessEqual(compact[index], BOUND, f"dispatch {index + 1}")
        self.assertEqual(
            compact, [sample["total_tokens"] for sample in evidence["context_samples"]]
        )
        self.assertEqual(
            [
                row["response"]["usage"]["prompt_tokens"]
                for row in evidence["model_rows"]
            ],
            [sample["provider_input_tokens"] for sample in evidence["context_samples"]],
        )
        self.assertGreater(baseline[-1], BOUND)
        self.assertLess(compact[-1] - compact[-11], baseline[-1] - baseline[-11])
        report = os.environ.get("ATLAS_CONTEXT_REPLAY_REPORT")
        if report:
            Path(report).write_text(
                json.dumps(
                    dict(
                        mode="genuine-provider-replay",
                        bound=BOUND,
                        cleared=compact,
                        provider_input=[
                            sample["provider_input_tokens"]
                            for sample in evidence["context_samples"]
                        ],
                        reconstructed_uncleared=baseline,
                        clearing_active=active,
                        reopened_ids=store.opened,
                        provider_completion_ids=ids,
                    ),
                    indent=2,
                )
                + "\n"
            )
