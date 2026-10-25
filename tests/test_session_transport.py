"""Captured long-session broker overflow; the transport cap must stay unchanged."""

from copy import deepcopy
import gzip
import json
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import AsyncMock, MagicMock

from packages.session_context import transport_context
from services.orchestrator.chat_schema import ChatMessage
from services.orchestrator.session_context import assemble, SessionContext
from services.orchestrator import session_updates
from services.retrieval.project_transcript import Transcript
from services.retrieval.project_work_contract import validate


class SessionTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_captured_overflow_is_bounded_before_broker_transmission(
        self,
    ) -> None:
        captured = json.loads(
            gzip.decompress(
                Path("tests/fixtures/session-transport.json.gz").read_bytes()
            )
        )
        context = captured["context"]
        original = deepcopy(context)
        transcript = object.__new__(Transcript)
        transcript.org, transcript.project = "org", "project"
        transcript.db = MagicMock()
        transcript.db.execute.return_value.fetchall.return_value = []
        result = transcript.state(captured["turn"], context, True)
        bounded = result["context"]
        validate("Context", bounded)
        self.assertLess(len(json.dumps(result).encode()), 160000)
        self.assertLess(len(bounded["history"]), len(context["history"]))
        self.assertEqual(context, original)
        self.assertEqual(bounded["session_state"], context["session_state"])
        self.assertEqual(bounded["facts"], context["facts"])
        ids = bounded["history_compaction"]["turn_ids"]
        self.assertTrue(set(ids) <= set(context["represented_turn_ids"]))
        history, dropped = assemble(
            bounded, ChatMessage(role="user", content="Continue")
        )
        broker = AsyncMock()
        controller = SessionContext(broker, result, dropped)
        messages: list[dict[str, object]] = [
            dict(role=m.role, content=m.text) for m in history
        ]
        await controller.prepare(messages, [], "atlas-qwen")
        broker.call.assert_awaited_once()
        self.assertEqual(broker.call.await_args.args[1]["compaction"]["turn_ids"], ids)
        if session_updates.tasks:
            import asyncio

            await asyncio.gather(*list(session_updates.tasks))

    def test_unrepresented_history_is_not_discarded_by_transport(self) -> None:
        context: dict[str, Any] = dict(
            session_state={},
            represented_turn_ids=[],
            history=[
                dict(id=str(i), content_refs=[dict(kind="question", text="x" * 1000)])
                for i in range(20)
            ],
        )
        self.assertEqual(transport_context(context), context)
