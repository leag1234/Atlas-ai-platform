import copy
import json
import logging
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.retrieval.project_identity import ProjectError


PROJECT = "00000000-0000-0000-0000-000000000001"
CONVERSATION = "00000000-0000-0000-0000-000000000002"
SESSION = "00000000-0000-0000-0000-000000000003"


class ProjectTurnReplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        root = Path("contracts/examples/project-mode")
        self.state: dict[str, Any] = json.loads(
            (root / "TranscriptState.valid.json").read_text()
        )
        self.state["turn"].update(
            id="turn-id",
            request_id="request-id",
            conversation_id=CONVERSATION,
            action="work",
            state="running",
            content_refs=[],
        )
        self.state["context"].update(
            instructions_text="Use the selected reports.", facts=[], history=[]
        )

    def request(self) -> ChatRequest:
        request = ChatRequest(
            project_id=PROJECT,
            org_id=PROJECT,
            conversation_id=CONVERSATION,
            project_action="work",
            work_session_id=SESSION,
            messages=[
                ChatRequest.model_validate(
                    {"messages": [{"role": "user", "content": "Compare reports"}]}
                ).messages[0]
            ],
            stream=True,
        )
        request._project_subject, request._transcript_key = "editor", "request-key"
        return request

    def broker(self, *, replay_error: bool) -> AsyncMock:
        replay = ProjectError(503, "dependency_unavailable")
        calls = AsyncMock(
            side_effect=[self.state, self.state["turn"], replay]
            if replay_error
            else [self.state, self.state["turn"], self.replay()]
        )
        return calls

    def replay(self) -> dict[str, Any]:
        value = copy.deepcopy(self.state)
        value["turn"]["state"] = "completed"
        value["events"] = [
            {
                "type": "recap",
                "turn_id": value["turn"]["id"],
                "text": "terminal recap",
            }
        ]
        return value

    async def run_turn(
        self, *, replay_error: bool, work: Any, sink: Any = None
    ) -> tuple[AsyncMock, Interaction]:
        from services.orchestrator import project_transcript
        from services.orchestrator.stream_client import sink_context

        item = Interaction()
        calls = self.broker(replay_error=replay_error)
        token = sink_context.set(sink)
        try:
            with (
                patch(
                    "services.orchestrator.project_transcript.TranscriptBroker.call",
                    calls,
                ),
                patch(
                    "services.orchestrator.code_mode.process_work",
                    side_effect=work,
                ),
            ):
                await project_transcript.process_scoped_project(self.request(), item)
        finally:
            sink_context.reset(token)
        return calls, item

    async def test_failed_replay_preserves_cost_failure_without_scheduling(
        self,
    ) -> None:
        async def work(request: ChatRequest, item: Interaction, **kwargs: Any) -> None:
            item.state = "failed"
            item.work_failure = {"category": "budget", "answer": ""}
            item.reponse = "Work failed: cost"

        with (
            patch("services.orchestrator.session_updates.schedule") as scheduled,
            self.assertLogs(
                "services.orchestrator.project_transcript", logging.WARNING
            ) as logs,
        ):
            calls, item = await self.run_turn(replay_error=True, work=work)
            self.assertIn("project_turn_replay_failed", "\n".join(logs.output))
            scheduled.assert_not_called()
        self.assertEqual(calls.await_count, 3)
        self.assertEqual(calls.await_args_list[1].args[0], "finish")
        self.assertEqual(calls.await_args_list[1].args[1]["state"], "failed")
        self.assertEqual(
            item.reponse,
            "The work did not complete: the budget limit for this turn was reached.",
        )

    async def test_failed_replay_schedules_completed_turn(self) -> None:
        async def work(request: ChatRequest, item: Interaction, **kwargs: Any) -> None:
            item.state = "done"
            item.reponse = "Reports compared."

        with (
            patch("services.orchestrator.session_updates.schedule") as scheduled,
            self.assertLogs(
                "services.orchestrator.project_transcript", logging.WARNING
            ) as logs,
        ):
            calls, item = await self.run_turn(replay_error=True, work=work)
            self.assertIn("project_turn_replay_failed", "\n".join(logs.output))
            scheduled.assert_called_once()
        self.assertEqual(calls.await_count, 3)
        self.assertEqual(item.state, "done")
        self.assertEqual(
            scheduled.call_args.args[1],
            {"turn": dict(self.state["turn"], state="completed")},
        )

    async def test_successful_replay_sends_terminal_recap(self) -> None:
        async def work(request: ChatRequest, item: Interaction, **kwargs: Any) -> None:
            item.state = "done"
            item.reponse = "Reports compared."

        events: list[dict[str, Any]] = []

        async def sink(event: dict[str, Any]) -> None:
            events.append(event)

        with patch("services.orchestrator.session_updates.schedule") as scheduled:
            calls, item = await self.run_turn(replay_error=False, work=work, sink=sink)
            scheduled.assert_called_once()
        self.assertEqual(calls.await_count, 3)
        self.assertEqual(item.state, "done")
        self.assertEqual(events, [{"project": self.replay()["events"][0]}])


if __name__ == "__main__":
    unittest.main()
