"""A durable memory receipt is acknowledged without another model execution."""

import unittest
from unittest.mock import AsyncMock
from services.orchestrator.project_transcript import saved_memory_reply


class MemoryReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_and_proposed_receipts_are_distinct(self) -> None:
        broker = AsyncMock()
        for status, expected in [
            ("active", "enregistré"),
            ("proposed", "attente de confirmation"),
        ]:
            broker.public.return_value = (
                200,
                {
                    "items": [
                        {
                            "source_turn_id": "turn",
                            "text": "budget 2000 euros",
                            "status": status,
                        }
                    ]
                },
            )
            reply = await saved_memory_reply(
                broker,
                {"id": "turn", "request_id": "request"},
                "Retiens que budget 2000 euros",
                "fr",
            )
            self.assertIn(expected, str(reply))
            self.assertIn("2000 euros", str(reply))
        self.assertIsNone(
            await saved_memory_reply(
                broker,
                {"id": "other", "request_id": "request"},
                "Retiens que budget 2000 euros",
                "fr",
            )
        )
        self.assertIsNone(
            await saved_memory_reply(
                broker,
                {"id": "turn", "request_id": "request"},
                "Quel est le budget ?",
                "fr",
            )
        )

    async def test_saved_reply_releases_work_lease_without_calling_model(self) -> None:
        from unittest.mock import MagicMock, patch
        from uuid import uuid4
        from services.orchestrator.code_mode import process_work, CodeModel
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.interactions import Interaction

        session_id, conversation, org, project = (str(uuid4()) for _ in range(4))
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
        execution.snapshot = AsyncMock()
        execution.broker = broker
        workspace = MagicMock()
        workspace.prepare = AsyncMock()
        workspace.cleanup = AsyncMock()
        execution.workspace.return_value = workspace
        request = ChatRequest.model_validate(
            dict(
                messages=[dict(role="user", content="Retiens ce budget")],
                conversation_id=conversation,
                project_id=project,
            )
        )
        item = Interaction()
        with (
            patch("services.orchestrator.code_workspace.enabled", return_value=True),
            patch(
                "services.orchestrator.code_api.ExecutionBroker", return_value=broker
            ),
            patch(
                "services.orchestrator.code_mode.WorkExecution", return_value=execution
            ),
            patch.object(CodeModel, "connect", AsyncMock()) as connect,
        ):
            await process_work(
                request,
                item,
                subject="editor",
                org_id=org,
                project_id=project,
                session_id=session_id,
                saved_reply="C’est enregistré.",
            )
        self.assertEqual(item.state, "done")
        self.assertEqual(item.reponse, "C’est enregistré.")
        self.assertEqual(execution.turn.await_args.args, ("release",))
        self.assertEqual(
            execution.turn.await_args.kwargs["terminal_state"], "completed"
        )
        self.assertEqual(execution.turn.await_args.kwargs["usage"]["model_calls"], 0)
        workspace.cleanup.assert_awaited_once()
        connect.assert_not_awaited()
