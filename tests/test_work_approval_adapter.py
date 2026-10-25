"""REQ-PROJ-05: flat model arguments retain strict approval and failure semantics."""

import json
import unittest
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from services.orchestrator.code_tools import approval_arguments, declarations, WorkTools
from services.orchestrator.code_mode import CodeModel, process_work
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call, CodeResult


class ApprovalAdapterTests(unittest.IsolatedAsyncioTestCase):
    def effects(self) -> dict[str, dict[str, Any]]:
        return {
            "delete_file": dict(
                file_id=str(uuid4()), base_version=str(uuid4()), revision=3
            ),
            "overwrite": dict(
                file_id=str(uuid4()),
                folder_id=str(uuid4()),
                path="note.txt",
                base_version=str(uuid4()),
                revision=3,
                sha256="a" * 64,
                size_bytes=42,
            ),
            "delete_memory": dict(memory_ids=[str(uuid4())], memory_revision=3),
        }

    def test_each_mapping_is_exact_and_strict_interface_is_unchanged(self) -> None:
        for kind, effects in self.effects().items():
            with self.subTest(kind=kind):
                expected = dict(
                    action_kind="overwrite" if kind == "overwrite" else "delete",
                    effects=effects,
                    maximum_bytes=42,
                )
                flat = dict(effect_kind=kind, maximum_bytes=42, **effects)
                self.assertEqual(approval_arguments(json.dumps(flat)), expected)
                self.assertEqual(approval_arguments(json.dumps(expected)), expected)

    def test_flat_declaration_has_no_polymorphic_effects(self) -> None:
        declaration = next(
            t["function"]
            for t in declarations(scoped=True)
            if isinstance(t["function"], dict)
            and t["function"]["name"] == "request_approval"
        )
        assert isinstance(declaration, dict)
        schema = declaration["parameters"]
        self.assertNotIn("effects", schema["properties"])
        self.assertEqual(
            schema["properties"]["effect_kind"]["enum"],
            ["overwrite", "delete_file", "delete_memory"],
        )
        self.assertNotIn("oneOf", json.dumps(schema))
        self.assertFalse(schema["additionalProperties"])

    async def test_bad_mapping_never_issues_approval_or_echoes_input(self) -> None:
        flat = dict(
            effect_kind="delete_file", maximum_bytes=42, **self.effects()["delete_file"]
        )
        bad = [
            dict(flat, effect_kind="publish"),
            dict(flat, revision=True),
            dict(flat, revision="3"),
            dict(flat, maximum_bytes=-1),
            dict(flat, folder_id=str(uuid4())),
            dict(flat, memory_ids=[]),
            dict(flat, file_id=None),
            dict(flat, authority="synthetic-secret"),
            {k: v for k, v in flat.items() if k != "base_version"},
            dict(
                effect_kind="delete_memory",
                maximum_bytes=0,
                memory_ids="[]",
                memory_revision=0,
            ),
            dict(
                effect_kind="overwrite",
                maximum_bytes=42,
                **dict(self.effects()["overwrite"], sha256="invalid"),
            ),
        ]
        for value in bad:
            with self.subTest(value=value):
                execution = MagicMock()
                execution.check = AsyncMock()
                execution.broker.call = AsyncMock(return_value={"events": []})
                tools = WorkTools(MagicMock(), execution)
                result = await tools.execute(
                    Call("bad", "request_approval", json.dumps(value)), 1
                )
                self.assertEqual(result["error"], "invalid_tool_arguments")
                self.assertNotIn("synthetic-secret", str(result))
                self.assertEqual(
                    [c.args[0] for c in execution.broker.call.await_args_list],
                    ["events"],
                )
                self.assertIn("request_approval", tools.validation_failures)

    async def test_work_terminal_matches_stop_and_unresolved_validation(self) -> None:
        for state, stop, tool_error, expected in (
            ("done", "", False, "completed"),
            ("stopped", "loop_detected", False, "failed"),
            ("done", "", True, "failed"),
        ):
            with self.subTest(state=state, stop=stop, tool_error=tool_error):
                session_id, conversation, org, project = (
                    str(uuid4()) for _ in range(4)
                )
                session = dict(
                    id=session_id,
                    conversation_id=conversation,
                    project_id=project,
                    revision=0,
                    effective_limits=dict(
                        expanded_bytes=100,
                        tool_calls=10,
                        model_calls=11,
                        wall_seconds=30,
                        cost_eur=0.3,
                        per_call_eur=0.1,
                    ),
                    base_file_versions=[],
                    environment={},
                )
                broker = MagicMock(org=org, project=project)
                broker.public = AsyncMock(return_value=(200, session))
                broker.call = AsyncMock(return_value=dict(session=session, events=[]))
                execution = MagicMock(session=session)
                execution.turn = AsyncMock()
                execution.check = AsyncMock()
                execution.broker = broker
                workspace = MagicMock()
                workspace.prepare = AsyncMock()
                workspace.cleanup = AsyncMock()
                execution.workspace.return_value = workspace
                model = MagicMock(spec=CodeModel)
                model.spent = Decimal(0)
                model.input_tokens = model.output_tokens = 0
                model.work_unknown = False
                request = ChatRequest.model_validate(
                    dict(
                        messages=[dict(role="user", content="Write a memo")],
                        conversation_id=conversation,
                        project_id=project,
                    )
                )
                item = Interaction()

                async def turn(*args: Any) -> CodeResult:
                    if tool_error:
                        await args[2].emit(
                            "tool",
                            tool="request_approval",
                            status="failed",
                            output={"error": "invalid_tool_arguments"},
                        )
                    return CodeResult(text="Partial work", state=state, reason=stop)

                with (
                    patch(
                        "services.orchestrator.code_workspace.enabled",
                        return_value=True,
                    ),
                    patch(
                        "services.orchestrator.code_api.ExecutionBroker",
                        return_value=broker,
                    ),
                    patch(
                        "services.orchestrator.code_mode.WorkExecution",
                        return_value=execution,
                    ),
                    patch.object(CodeModel, "connect", AsyncMock(return_value=model)),
                    patch(
                        "services.orchestrator.code_mode.execute_turn", side_effect=turn
                    ),
                ):
                    await process_work(
                        request,
                        item,
                        subject="editor",
                        org_id=org,
                        project_id=project,
                        session_id=session_id,
                    )
                self.assertEqual(
                    execution.turn.await_args.kwargs["terminal_state"], expected
                )
                recap = broker.call.await_args.args[1]["progress"][0]
                self.assertEqual(recap["status"], expected)
                self.assertEqual(
                    item.state, "done" if expected == "completed" else "failed"
                )
                if expected == "failed":
                    self.assertIn(stop or "invalid_tool_arguments", item.reponse)
                    self.assertIn(stop or "invalid_tool_arguments", item.erreurs)
                workspace.cleanup.assert_awaited_once()
