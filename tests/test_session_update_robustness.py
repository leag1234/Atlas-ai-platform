"""Captured P1b provider defects and strict normalization/retry boundaries."""

import asyncio
import json
import os
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from pydantic import ValidationError

from packages.session_state import SessionState
from services.orchestrator.memory import consolidated
from services.orchestrator.model import GatewayModel
from services.orchestrator import session_updates


def update_value() -> dict[str, Any]:
    return SessionState.empty().model_dump() | dict(
        resolved_questions=[],
        replace_plan=False,
        established_facts=[
            dict(id="units", text="48", src="user", turn_id=str(uuid4()))
        ],
    )


class UpdateParsingTests(unittest.TestCase):
    def test_exact_live_chat_alias_shape(self) -> None:
        capture = json.loads(
            Path("tests/fixtures/session-update-alias.json").read_text()
        )
        answer, facts = consolidated(capture["response"], capture["question"])
        original = json.loads(capture["response"])
        self.assertEqual(answer, original["answer"])
        self.assertEqual(facts, [f["text"] for f in original["facts"]])
        with self.assertRaises(ValueError):
            consolidated(capture["response"], "unrelated question")

    def test_content_json_and_fenced_extraction_source_alias(self) -> None:
        value = update_value()
        for text in (json.dumps(value), "```json\n" + json.dumps(value) + "\n```"):
            parsed = session_updates.parse_update(dict(text=text, calls=[]))
            self.assertEqual(parsed.established_facts[0].source, "user")
            self.assertEqual(parsed.established_facts[0].text, "48")
        self.assertIn("src", value["established_facts"][0])

    def test_stop_with_valid_tool_payload_and_invalid_payload(self) -> None:
        from streaming import StreamDecoder

        # P1b-live-2 diagnostics: tool_calls=1, finish_reason=stop.
        # Its recorder stored only provider_error, not the rejected wire payload.
        value = update_value()
        call = dict(
            index=0,
            id="state",
            type="function",
            function=dict(name="update_session_state", arguments=json.dumps(value)),
        )
        frame: dict[str, Any] = dict(
            choices=[
                dict(index=0, delta=dict(tool_calls=[call]), finish_reason="stop")
            ],
            usage=dict(prompt_tokens=100, completion_tokens=100),
        )
        decoder = StreamDecoder(allow_length=True)
        decoder.feed(("data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n").encode())
        decoder.finish()
        self.assertIsNotNone(decoder.result)
        parsed = session_updates.parse_update(decoder.result)
        self.assertEqual(parsed.established_facts[0].source, "user")
        with_text = dict(decoder.result or {}, text="State updated.")
        self.assertEqual(session_updates.parse_update(with_text), parsed)
        frame["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] = (
            '{"bad": true}'
        )
        decoder = StreamDecoder(allow_length=True)
        decoder.feed(("data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n").encode())
        with self.assertRaises(ValidationError):
            session_updates.parse_update(decoder.result)

    def test_strict_validation_after_aliases(self) -> None:
        for change in (
            {"source": "other"},
            {"turn_id": "bad"},
            {"surprise": 1},
            {"src": 1},
        ):
            value = update_value()
            value["established_facts"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                session_updates.parse_update(dict(text=json.dumps(value), calls=[]))
        for text in (
            '{"replace_plan":false,"replace_plan":true}',
            "{} {}",
            "[]",
            "not json",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                session_updates.parse_update(dict(text=text, calls=[]))
        value = update_value()
        value["replace_plan"] = "false"
        with self.assertRaises(ValidationError):
            session_updates.parse_update(dict(text=json.dumps(value), calls=[]))

    def test_chat_alias_collision_and_unknown_fields_remain_invalid(self) -> None:
        capture = json.loads(
            Path("tests/fixtures/session-update-alias.json").read_text()
        )
        for field in ("evidence", "unknown"):
            value = json.loads(capture["response"])
            value["facts"][0][field] = value["facts"][0]["e"]
            with self.subTest(field=field), self.assertRaises(ValueError):
                consolidated(json.dumps(value), capture["question"])

    def test_json_mode_capability_keeps_unknown_provider_fallback(self) -> None:
        import provider_settings
        from agent_provider import AgentRequest

        payload = dict(
            messages=[dict(role="user", content="JSON")],
            tools=[],
            timeout=30.0,
            response_format=dict(type="json_object"),
        )
        request = AgentRequest.model_validate(payload)
        self.assertEqual(request.response_format, {"type": "json_object"})
        for provider in ("mistral", "local", "ovhcloud", "scaleway"):
            with patch.dict(os.environ, ATLAS_PROVIDER=provider):
                body = provider_settings.json_options(request.response_format)
            self.assertEqual(body["response_format"], {"type": "json_object"})
        for provider in ("hetzner", "other"):
            with patch.dict(os.environ, ATLAS_PROVIDER=provider):
                body = provider_settings.json_options(request.response_format)
            self.assertNotIn("response_format", body)


class UpdateRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_preserves_state_and_next_turn_retries_once(self) -> None:
        broker = AsyncMock()
        broker.org, broker.project = str(uuid4()), str(uuid4())
        first, second, conversation = str(uuid4()), str(uuid4()), str(uuid4())
        old = SessionState.empty().model_dump()
        states = [
            dict(
                session_state=old,
                turns=[dict(id=first)],
                represented_turn_ids=[],
                previous_turn_id=None,
                session_id=None,
                budget_eur=0.01,
            ),
            dict(
                session_state=old,
                turns=[dict(id=first), dict(id=second)],
                represented_turn_ids=[],
                previous_turn_id=None,
                session_id=None,
                budget_eur=0.01,
            ),
        ]
        writes = []

        async def call(operation: str, payload: Any, *args: Any) -> Any:
            if operation == "state-read":
                return states.pop(0)
            if operation == "state-write":
                writes.append(payload)
            return {}

        broker.call.side_effect = call
        valid = update_value()
        valid["established_facts"][0]["turn_id"] = first
        post = AsyncMock(
            side_effect=[
                dict(text='{"bad":true}', calls=[]),
                dict(text=json.dumps(valid), calls=[], usage={}, cost_eur=0),
            ]
        )
        completed = dict(
            turn=dict(id=first, request_id=first, conversation_id=conversation)
        )
        with (
            patch.object(GatewayModel, "post", post),
            patch(
                "services.orchestrator.project_transcript.DispatchEvents.publish",
                AsyncMock(),
            ),
            patch.dict(os.environ, ATLAS_GATEWAY_URL="http://127.0.0.1:42610"),
        ):
            with self.assertLogs(session_updates.log, level="WARNING") as logs:
                await session_updates.update(broker, completed)
            self.assertEqual(writes, [])
            self.assertEqual(post.await_count, 1)
            self.assertIn("reason=invalid_schema", logs.output[0])
            self.assertEqual(old, SessionState.empty().model_dump())
            await asyncio.sleep(0)
            self.assertEqual(post.await_count, 1)
            completed["turn"].update(id=second, request_id=second)
            await session_updates.update(broker, completed)
        self.assertEqual(post.await_count, 2)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["represented_turn_ids"], [first, second])
        payload = post.call_args.args[1]
        self.assertEqual(payload["tools"], [])
        self.assertNotIn("tool_choice", payload)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(
            json.loads(payload["messages"][-1]["content"])["previous_session_state"],
            old,
        )
