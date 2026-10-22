"""Only the project answer is visible while consolidation JSON is still arriving."""

import json
import unittest

from services.orchestrator.project_stream import AnswerStream


class AnswerStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_answer_without_internal_facts(self) -> None:
        events: list[dict[str, object]] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        stream = AnswerStream(sink)
        answer = 'Bonjour "ami" 👋\nSuite'
        wire = json.dumps(
            {"facts": [{"text": "PRIVATE", "evidence": "PRIVATE"}], "answer": answer}
        )
        for char in wire[:-1]:
            await stream({"delta": {"content": char}})
        visible = "".join(
            str(delta["content"])
            for e in events
            if isinstance(delta := e["delta"], dict)
        )
        self.assertEqual(visible, answer)
        self.assertNotIn("PRIVATE", visible)
        self.assertGreater(len(events), 1)
        await stream({"delta": {"content": wire[-1]}})
        self.assertEqual(
            "".join(
                str(delta["content"])
                for e in events
                if isinstance(delta := e["delta"], dict)
            ),
            answer,
        )

    async def test_reset_reasoning_and_limits(self) -> None:
        events: list[dict[str, object]] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        stream = AnswerStream(sink)
        await stream({"delta": {"content": '{"answer":"old"'}})
        await stream({"phase": "generating", "turn": 2})
        await stream({"delta": {"reasoning_content": "reasoning"}})
        await stream({"delta": {"content": '{"answer":"new","facts":[]}'}})
        self.assertEqual(events[-1], {"delta": {"content": "new"}})
        self.assertIn({"delta": {"reasoning_content": "reasoning"}}, events)
        with self.assertRaises(ValueError):
            await stream({"delta": {"content": "x" * 32001}})

    async def test_project_pipeline_streams_validated_answer(self) -> None:
        from unittest.mock import patch
        from test_project_chat import ProjectChatTests
        from services.orchestrator.stream_client import Sink, sink_context

        case = ProjectChatTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        events: list[dict[str, object]] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        async def receive(
            url: str, payload: dict[str, object], timeout: float, emit: Sink
        ) -> dict[str, object]:
            wire = await case.post(
                url.replace("/agent/stream", "/agent/complete"), payload, timeout
            )
            text = str(wire["text"])
            split = text.index(', "facts"')
            await emit({"delta": {"content": text[:split]}})
            self.assertTrue(any("delta" in event for event in events))
            await emit({"delta": {"content": text[split:]}})
            return wire

        token = sink_context.set(sink)
        try:
            with patch("services.orchestrator.model.receive", side_effect=receive):
                item = await case.ask(
                    case.first,
                    case.conversation(case.first),
                    "Mon projet utilise Python.",
                )
            visible = "".join(
                str(delta.get("content", ""))
                for e in events
                if isinstance(delta := e.get("delta"), dict)
            )
            self.assertEqual(item.state, "done")
            self.assertEqual(visible, item.reponse)
            self.assertNotIn('"facts"', visible)
        finally:
            sink_context.reset(token)
