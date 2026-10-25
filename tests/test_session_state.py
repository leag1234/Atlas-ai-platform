"""REQ-PROJ-04/05: validated memory, bounded assembly and provider calibration."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import os
from typing import Any
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from pydantic import ValidationError

from packages import context_usage
from packages.session_context import Calibration, recent, threshold
from packages.session_state import SessionState, StateUpdate, merge
from services.orchestrator.chat_schema import ChatMessage
from services.orchestrator.session_context import assemble, SessionContext
from services.orchestrator import session_updates
from services.retrieval.project_work_contract import validate


class SessionStateTests(unittest.TestCase):
    def test_strict_schema_and_additive_contract(self) -> None:
        state = SessionState.empty().model_dump()
        validate("SessionState", state)
        for change in (
            {"unexpected": True},
            {"decisions": "text"},
            {"current_plan": [dict(id="x", text="plan", source="turn", turn_id=7)]},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                SessionState.model_validate(state | change)
        entry = dict(
            id="allocation",
            text="48 units at EUR 37.25",
            source="User turn",
            turn_id=str(uuid4()),
        )
        with self.assertRaises(ValidationError):
            SessionState.model_validate(state | dict(established_facts=[entry, entry]))

    def test_merge_preserves_omissions_corrects_and_resolves_explicitly(self) -> None:
        turn = str(uuid4())
        fact = dict(id="units", text="48", source="User", turn_id=turn)
        old = SessionState.model_validate(
            SessionState.empty().model_dump()
            | dict(established_facts=[fact], open_questions=[fact], current_plan=[fact])
        )
        update = StateUpdate.model_validate(
            SessionState.empty().model_dump()
            | dict(resolved_questions=[], replace_plan=False)
        )
        self.assertEqual(merge(old, update, {turn}), old)
        update.established_facts = [
            type(old.established_facts[0]).model_validate(fact | dict(text="49"))
        ]
        update.resolved_questions = ["units"]
        update.replace_plan = True
        result = merge(old, update, {turn})
        self.assertEqual(result.established_facts[0].text, "49")
        self.assertEqual(result.current_plan, [])
        self.assertEqual(result.open_questions, [])
        with self.assertRaises(ValueError):
            merge(old, update, set())
        self.assertEqual(old.established_facts[0].text, "48")

    def test_calibration_prefers_exact_observation_and_is_family_scoped(self) -> None:
        calibration = Calibration()
        self.assertEqual(calibration.count("a", "first", 300), 400)
        calibration.observe("a", "first", 300, 450)
        self.assertEqual(calibration.count("a", "first", 300), 450)
        self.assertEqual(calibration.count("a", "changed", 600), 900)
        self.assertEqual(calibration.count("b", "changed", 600), 800)
        calibration.observe("a", "small", 300, 200)
        self.assertEqual(calibration.count("a", "small", 300), 200)
        self.assertEqual(calibration.count("a", "other", 300), 450)

    def test_threshold_edges(self) -> None:
        for count, expected in [
            (599, "normal"),
            (600, "compact"),
            (799, "compact"),
            (800, "warning"),
            (949, "warning"),
            (950, "refuse"),
        ]:
            self.assertEqual(threshold(count, 1000), expected)

    def test_unrepresented_turns_are_never_dropped(self) -> None:
        turns: list[dict[str, Any]] = [
            dict(id=str(uuid4()), content_refs=[dict(kind="question", text=str(i))])
            for i in range(20)
        ]
        self.assertEqual(recent(turns, set()), (turns, []))
        kept, dropped = recent(turns, {t["id"] for t in turns})
        self.assertEqual(kept, turns[-8:])
        self.assertEqual(dropped, turns[:-8])
        with patch.dict(os.environ, ATLAS_CONTEXT_RECENT_TOKENS="1"):
            self.assertEqual(recent(turns, {t["id"] for t in turns})[0], turns[-1:])


class SessionAssemblyTests(unittest.IsolatedAsyncioTestCase):
    async def test_stable_prefix_state_history_tools_documents_and_three_compactions(
        self,
    ) -> None:
        token = context_usage.parts.set({})
        self.addCleanup(context_usage.parts.reset, token)
        constraint = (
            "Ship only to France or Germany. Never ship outside these countries."
        )
        context: dict[str, Any] = dict(
            instructions_text="Exact instructions\n",
            facts=[dict(kind="constraint", text=constraint)],
            passages=[dict(text="document")],
            context_manifest=dict(coverage={}),
            history=[],
            represented_turn_ids=[],
            session_state=SessionState.empty().model_dump(),
        )
        broker = AsyncMock()
        broker.call.return_value = dict(events=[])
        prefix = None
        for number in range(12):
            if number:
                context["history"].append(
                    dict(
                        id=str(uuid4()),
                        content_refs=[
                            dict(kind="question", text=f"Question {number}"),
                            dict(kind="answer", text=f"Answer {number}"),
                        ],
                    )
                )
            context["represented_turn_ids"] = [t["id"] for t in context["history"]]
            history, dropped = assemble(
                context, ChatMessage(role="user", content="Next")
            )
            messages: list[dict[str, object]] = [
                dict(role="system", content="System\n")
            ]
            for message in history:
                messages.append(dict(role=message.role, content=message.text))
            messages.extend(
                [
                    dict(role="assistant", content="", tool_calls=[dict(id="tool")]),
                    dict(role="tool", tool_call_id="tool", content="result"),
                ]
            )
            controller = SessionContext(
                broker,
                dict(
                    context=deepcopy(context),
                    turn=dict(request_id=str(uuid4()), conversation_id=str(uuid4())),
                ),
                dropped,
            )
            sample = await controller.prepare(
                messages, [dict(type="function")], "atlas-qwen"
            )
            self.assertIn(constraint, str(messages[:3]))
            current = messages[:3]
            if prefix is not None:
                self.assertEqual(current, prefix)
            prefix = current
            self.assertIn("session_state", str(messages[3]))
            self.assertEqual(messages[-3]["role"], "tool")
            self.assertIn("document_passages", str(messages[-2]))
            self.assertEqual(messages[-1], dict(role="user", content="Next"))
            self.assertGreater(sample["calibrated_tokens"], sample["total_tokens"])
        self.assertEqual(broker.call.await_count, 3)

    async def test_background_update_does_not_block_and_failure_preserves_state(
        self,
    ) -> None:
        blocked = asyncio.Event()
        broker = AsyncMock()
        broker.org, broker.project = str(uuid4()), str(uuid4())

        async def wait(*args: Any, **kwargs: Any) -> Any:
            await blocked.wait()
            raise ValueError("invalid update")

        broker.call.side_effect = wait
        completed = dict(
            turn=dict(
                id=str(uuid4()), request_id=str(uuid4()), conversation_id=str(uuid4())
            )
        )
        session_updates.schedule(broker, completed)
        self.assertTrue(session_updates.tasks)
        await asyncio.sleep(0)
        self.assertEqual(broker.call.await_count, 1)
        blocked.set()
        with self.assertLogs(session_updates.log, level="WARNING") as logs:
            await asyncio.gather(*session_updates.tasks)
        self.assertIn("session_state_update_failed", logs.output[0])
        self.assertEqual(broker.call.await_count, 1)

    async def test_threshold_compacts_history_but_refuses_pinned_overflow(self) -> None:
        from services.retrieval.project_identity import ProjectError

        token = context_usage.parts.set({})
        self.addCleanup(context_usage.parts.reset, token)
        turns: list[dict[str, Any]] = [
            dict(
                id=str(uuid4()),
                content_refs=[
                    dict(kind="question", text=str(i) + "x" * 800),
                    dict(kind="answer", text="acknowledged"),
                ],
            )
            for i in range(3)
        ]
        context: dict[str, Any] = dict(
            instructions_text="Pinned shipping restriction",
            facts=[],
            passages=[],
            context_manifest=dict(coverage={}),
            session_state=SessionState.empty().model_dump(),
            history=turns,
            represented_turn_ids=[t["id"] for t in turns],
        )
        broker = AsyncMock()
        broker.call.return_value = dict(events=[])
        turn = dict(request_id=str(uuid4()), conversation_id=str(uuid4()))
        history, dropped = assemble(
            context, ChatMessage(role="user", content="Continue")
        )
        messages: list[dict[str, object]] = [dict(role="system", content="System")]
        for message in history:
            messages.append(dict(role=message.role, content=message.text))
        controller = SessionContext(broker, dict(context=context, turn=turn), dropped)
        with patch("packages.context_usage.context_window", return_value=1000):
            sample = await controller.prepare(messages, [], "atlas-qwen")
        self.assertLess(sample["calibrated_tokens"], 600)
        self.assertIn("Pinned shipping restriction", str(messages))
        self.assertNotIn(turns[0]["content_refs"][0]["text"], str(messages))
        self.assertIn(turns[-1]["content_refs"][0]["text"], str(messages))
        self.assertEqual(
            broker.call.await_args.args[1]["compaction"]["reason"], "threshold"
        )
        for length, expected in [(2400, "warning"), (3000, "refuse")]:
            protected: list[dict[str, object]] = [
                dict(role="system", content="p" * length),
                dict(role="user", content="Continue"),
            ]
            original = deepcopy(protected)
            controller = SessionContext(
                broker, dict(context=dict(history=[]), turn=turn), []
            )
            with patch("packages.context_usage.context_window", return_value=1000):
                if expected == "refuse":
                    with self.assertRaises(ProjectError) as rejection:
                        await controller.prepare(protected, [], "atlas-qwen")
                    self.assertEqual(rejection.exception.status, 413)
                    self.assertIn(
                        "new session", str(rejection.exception.body["message"])
                    )
                else:
                    sample = await controller.prepare(protected, [], "atlas-qwen")
                    self.assertEqual(sample["context_status"], "warning")
            self.assertEqual(protected, original)
