"""REQ-PROJ-04: prose answers are independent of best-effort state work."""

import asyncio
from copy import deepcopy
from decimal import Decimal
import json
import sys
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import AsyncMock, patch

from packages import context_usage
from packages.session_state import SessionState
from services.orchestrator import session_updates
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Turn
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.project_transcript import process_scoped_project

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_project_dispatch as dispatch


class AnswerIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, compact: bool) -> None:
        fixture = dispatch.ScopedProjectDispatchTests()
        state = fixture.state()
        state.update(replay=False, events=[])
        context = state["context"]
        context.update(
            session_state=SessionState.empty().model_dump(),
            represented_turn_ids=[],
            passages=[],
            context_manifest=dict(coverage=dict(complete=True, warnings=[])),
        )
        if compact:
            for number in range(3):
                context["history"].append(
                    dict(
                        id=str(number),
                        content_refs=[
                            dict(kind="question", text=str(number) + "x" * 4800)
                        ],
                    )
                )
            context["represented_turn_ids"] = [str(i) for i in range(3)]
        before = deepcopy(context["session_state"])
        writes = []
        compactions = []
        blocked = asyncio.Event()

        async def broker(operation: str, payload: Any, *args: Any) -> Any:
            if operation == "compact":
                compactions.append(payload)
                await blocked.wait()
                raise ValueError("unusable compaction")
            if operation == "finish":
                state["turn"]["state"] = payload["state"]
            if operation == "state-read":
                return dict(
                    session_state=before,
                    turns=[state["turn"]],
                    session_id=None,
                    budget_eur=0.01,
                )
            if operation == "state-write":
                writes.append(payload)
            return state

        model = GatewayModel(
            "http://gateway",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=100,
            ),
        )
        prompts = []

        async def complete(messages: Any, timeout: float) -> Turn:
            prompts.append(deepcopy(messages))
            return Turn("The approved total is EUR 1788 for 48 units.")

        token = context_usage.parts.set({})
        self.addCleanup(context_usage.parts.reset, token)
        with (
            patch(
                "services.orchestrator.project_transcript.TranscriptBroker.call",
                side_effect=broker,
            ),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(model, "complete", side_effect=complete),
            patch.object(
                GatewayModel,
                "post",
                AsyncMock(return_value=dict(text="I retained the figures.", calls=[])),
            ),
            patch(
                "services.orchestrator.project_transcript.DispatchEvents.publish",
                AsyncMock(),
            ),
            patch(
                "packages.context_usage.context_window",
                return_value=6000 if compact else 262144,
            ),
        ):
            item = Interaction()
            await asyncio.wait_for(process_scoped_project(fixture.request(), item), 1)
            self.assertEqual(item.state, "done")
            self.assertEqual(
                item.reponse, "The approved total is EUR 1788 for 48 units."
            )
            self.assertEqual(
                prompts[0][0]["content"].split("\n")[0],
                Path("prompts/chat.txt").read_text().split("\n")[0],
            )
            if compact:
                self.assertTrue(compactions)
                self.assertEqual(compactions[0]["compaction"]["reason"], "threshold")
                self.assertFalse(blocked.is_set())
            blocked.set()
            if session_updates.tasks:
                await asyncio.gather(*list(session_updates.tasks))
        self.assertEqual(writes, [])
        self.assertEqual(context["session_state"], before)

    async def test_background_prose_does_not_replace_state_or_answer(self) -> None:
        await self.exercise(compact=False)

    async def test_compaction_failure_at_sixty_percent_does_not_block_answer(
        self,
    ) -> None:
        await self.exercise(compact=True)

    def test_extract_first_valid_object_surrounded_by_prose(self) -> None:
        value = SessionState.empty().model_dump() | dict(
            resolved_questions=[], replace_plan=False
        )
        response = (
            "Here is the state.\n```json\n" + json.dumps(value) + "\n```\nFinished."
        )
        self.assertEqual(
            session_updates.parse_update(dict(text=response, calls=[])).model_dump(),
            value,
        )

    async def test_exact_captured_hetzner_prose_answer(self) -> None:
        from services.orchestrator.loop import Result

        fixture = dispatch.ScopedProjectDispatchTests()
        state = fixture.state()
        state["context"]["passages"] = []
        state["context"]["context_manifest"]["coverage"] = dict(
            complete=True, warnings=[]
        )
        captured = json.loads(
            Path("tests/fixtures/session-prose-answer.json").read_text()
        )
        answer = captured["response"]["text"]
        model = GatewayModel(
            "http://gateway",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=100,
            ),
        )
        with (
            patch(
                "services.orchestrator.project_transcript.TranscriptBroker.call",
                AsyncMock(side_effect=[state, state["turn"], dict(state, replay=True)]),
            ),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch(
                "services.orchestrator.project_transcript.run",
                AsyncMock(return_value=Result(text=answer, state="done")),
            ),
        ):
            item = Interaction()
            await process_scoped_project(fixture.request(), item)
        self.assertEqual(item.reponse, answer)
        self.assertEqual(item.state, "done")
        self.assertIn("37.25", item.reponse)
        self.assertIn("48", item.reponse)

    def test_optional_historical_envelope_never_requires_json(self) -> None:
        from services.orchestrator.project_transcript import answer_content

        for text in (
            "A useful prose answer.",
            "{malformed JSON",
            '{"answer":""}',
            '["answer"]',
        ):
            self.assertEqual(answer_content(text), text)
        self.assertEqual(
            answer_content('{"answer":"A useful answer.","facts":[]}'),
            "A useful answer.",
        )
