"""Tool-call arguments always leave as one valid JSON object (client report 2026-11-16).

The malformed replies below follow the slips seen from hosted models: a call cut before
its closing brace, a trailing comma, a code fence, Python-style quotes, a raw line break
inside a string, arguments encoded twice, and a declared tool called without a required
parameter.
"""

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from dev_gateway import Plan, corrected
from services.orchestrator import call_repair
from services.orchestrator.api_keys import Store
from services.orchestrator.call_repair import (
    InvalidToolArguments,
    checked_call,
    normalized_arguments,
    repaired_frames,
)
from services.orchestrator.chat_api import app

WRITE = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
    "required": ["path", "text"],
}
TOOLS = [{"type": "function", "function": {"name": "write_cell", "parameters": WRITE}}]
DECLARED = {"write_cell": WRITE}
# Recorded shapes of malformed arguments (fictional values).
CUT = '{"path": "B2", "text": "Quarterly total"'
VALID = '{"path": "B2", "text": "Quarterly total"}'


def call_frames(arguments: str, call_id: str = "call-1") -> list[dict[str, Any]]:
    pieces = [arguments[: len(arguments) // 2], arguments[len(arguments) // 2 :]]
    frames: list[dict[str, Any]] = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "Filling the cell."},
                    "finish_reason": None,
                }
            ]
        }
    ]
    for number, piece in enumerate(pieces):
        call: dict[str, Any] = {"index": 0, "function": {"arguments": piece}}
        if number == 0:
            call |= {"id": call_id, "type": "function"}
            call["function"]["name"] = "write_cell"
        frames.append(
            {
                "choices": [
                    {"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": None}
                ]
            }
        )
    frames.append(
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
    )
    frames.append(
        {
            "choices": [],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "cost_micro_eur": 30,
        }
    )
    return frames


async def source(frames: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    for frame in frames:
        yield frame


def collect(
    first: list[dict[str, Any]],
    *later: list[dict[str, Any]],
    limit: int | None = None,
    retry: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    notes: list[str] = []
    queue = list(later)

    def restart(note: str) -> AsyncIterator[dict[str, Any]]:
        notes.append(note)
        return source(queue.pop(0))

    async def run() -> list[dict[str, Any]]:
        return [
            frame
            async for frame in repaired_frames(
                source(first), TOOLS, restart if retry else None, limit
            )
        ]

    return asyncio.run(run()), notes


def sent_calls(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        call
        for frame in frames
        for choice in frame.get("choices") or []
        for call in choice["delta"].get("tool_calls") or []
    ]


def text(frames: list[dict[str, Any]]) -> str:
    return "".join(
        choice["delta"].get("content") or ""
        for frame in frames
        for choice in frame.get("choices") or []
    )


class NormalizeTests(unittest.TestCase):
    def test_valid_arguments_leave_exactly_as_sent(self) -> None:
        self.assertEqual(normalized_arguments(VALID), (VALID, None))
        self.assertEqual(normalized_arguments(' {"a": 1} '), (' {"a": 1} ', None))

    def test_safe_slips_are_rewritten_to_one_object(self) -> None:
        cases = {
            '{"path": "B2", "text": "x",}': (
                "trailing_comma",
                {"path": "B2", "text": "x"},
            ),
            '{"rows": [1, 2,], "sheet": "Q3",}': (
                "trailing_comma",
                {"rows": [1, 2], "sheet": "Q3"},
            ),
            '```json\n{"path": "B2"}\n```': ("code_fence", {"path": "B2"}),
            "{'path': 'B2', 'bold': True, 'note': None}": (
                "python_literal",
                {"path": "B2", "bold": True, "note": None},
            ),
            '{"text": "line one\nline two"}': (
                "control_character",
                {"text": "line one\nline two"},
            ),
            json.dumps(VALID): ("double_encoded", json.loads(VALID)),
            "": ("empty", {}),
            "   ": ("empty", {}),
        }
        for sent, (repair, value) in cases.items():
            with self.subTest(sent=sent):
                result = normalized_arguments(sent)
                assert result is not None
                self.assertEqual(result[1], repair)
                self.assertEqual(json.loads(result[0]), value)

    def test_commas_inside_strings_are_kept(self) -> None:
        result = normalized_arguments('{"text": "a, }", "list": [1,],}')
        assert result is not None
        self.assertEqual(json.loads(result[0]), {"text": "a, }", "list": [1]})

    def test_ambiguous_or_broken_arguments_are_not_guessed(self) -> None:
        for sent in (
            CUT,
            '{"path": "B2"}{"path": "C3"}',
            '{"path": "B2"} trailing words',
            "[1, 2]",
            '"just a string"',
            '{"value": NaN}',
            "{'path': ('B', 2)}",
            "{'path': __import__('os')}",
            "path=B2",
        ):
            with self.subTest(sent=sent):
                self.assertIsNone(normalized_arguments(sent))

    def test_missing_required_parameter_is_a_problem(self) -> None:
        call = {
            "index": 0,
            "id": "c",
            "name": "write_cell",
            "arguments": '{"path": "B2"}',
        }
        self.assertEqual(checked_call(call, DECLARED)[1], "missing_required")
        undeclared = dict(call, name="other_tool")
        self.assertIsNone(checked_call(undeclared, DECLARED)[1])
        self.assertEqual(
            checked_call(dict(call, arguments=CUT), DECLARED)[1], "invalid_json"
        )


class StreamTests(unittest.TestCase):
    def test_invalid_call_is_asked_again_once_and_usage_adds_up(self) -> None:
        out, notes = collect(call_frames(CUT), call_frames(VALID, "call-2"))
        self.assertEqual(len(notes), 1)
        self.assertIn("write_cell", notes[0])
        self.assertIn("not valid JSON", notes[0])
        self.assertNotIn("Quarterly", notes[0])
        calls = sent_calls(out)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["id"], "call-2")
        self.assertEqual(
            json.loads(calls[0]["function"]["arguments"]), json.loads(VALID)
        )
        # The text of the first attempt is shown once; the second adds only its call.
        self.assertEqual(text(out), "Filling the cell.")
        roles = [
            choice["delta"].get("role")
            for frame in out
            for choice in frame.get("choices") or []
            if choice["delta"].get("role")
        ]
        self.assertEqual(roles, ["assistant"])
        usage = [frame for frame in out if "usage" in frame]
        self.assertEqual(len(usage), 1)
        self.assertEqual(
            usage[0]["usage"], {"prompt_tokens": 200, "completion_tokens": 40}
        )
        self.assertEqual(usage[0]["cost_micro_eur"], 60)
        finishes = [
            choice["finish_reason"]
            for frame in out
            for choice in frame.get("choices") or []
            if choice["finish_reason"]
        ]
        self.assertEqual(finishes, ["tool_calls"])

    def test_missing_required_parameter_is_asked_again(self) -> None:
        out, notes = collect(
            call_frames('{"path": "B2"}'), call_frames(VALID, "call-2")
        )
        self.assertIn("required", notes[0])
        self.assertIn("text", notes[0])
        self.assertEqual(sent_calls(out)[0]["id"], "call-2")

    def test_still_missing_required_after_retry_is_returned_for_the_client(
        self,
    ) -> None:
        out, _ = collect(call_frames('{"path": "B2"}'), call_frames('{"path": "B3"}'))
        self.assertEqual(
            json.loads(sent_calls(out)[0]["function"]["arguments"]), {"path": "B3"}
        )

    def test_safe_slip_needs_no_second_attempt(self) -> None:
        out, notes = collect(call_frames('{"path": "B2", "text": "x",}'))
        self.assertEqual(notes, [])
        self.assertEqual(
            json.loads(sent_calls(out)[0]["function"]["arguments"]),
            {"path": "B2", "text": "x"},
        )

    def test_invalid_twice_never_leaves_and_both_usages_are_settled(self) -> None:
        received: list[dict[str, Any]] = []

        async def run() -> None:
            async for frame in repaired_frames(
                source(call_frames(CUT)),
                TOOLS,
                lambda note: source(call_frames(CUT, "call-2")),
            ):
                received.append(frame)

        with self.assertRaises(InvalidToolArguments):
            asyncio.run(run())
        self.assertEqual(sent_calls(received), [])
        usage = [frame for frame in received if "usage" in frame]
        self.assertEqual(usage[-1]["cost_micro_eur"], 60)

    def test_without_restart_or_reservation_invalid_call_is_refused(self) -> None:
        for kwargs in ({"retry": False}, {"limit": 50}):
            with self.subTest(**kwargs):
                received: list[dict[str, Any]] = []

                async def run() -> None:
                    notes: list[str] = []

                    def restart(note: str) -> AsyncIterator[dict[str, Any]]:
                        notes.append(note)
                        return source(call_frames(VALID))

                    async for frame in repaired_frames(
                        source(call_frames(CUT)),
                        TOOLS,
                        None if kwargs.get("retry") is False else restart,
                        kwargs.get("limit"),
                    ):
                        received.append(frame)
                    self.fail("no error raised")

                with self.assertRaises(InvalidToolArguments):
                    asyncio.run(run())
                self.assertEqual(sent_calls(received), [])
                # The first attempt's usage is still settled.
                self.assertEqual(
                    [f["cost_micro_eur"] for f in received if "usage" in f], [30]
                )

    def test_answer_without_call_on_retry_follows_the_shown_text(self) -> None:
        answer = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": "The cell is filled.",
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        out, _ = collect(call_frames(CUT), answer)
        self.assertEqual(text(out), "Filling the cell.\n\nThe cell is filled.")
        self.assertEqual(sent_calls(out), [])

    def test_answer_cut_by_max_tokens_is_returned_as_sent(self) -> None:
        frames = call_frames(CUT)
        frames[-2]["choices"][0]["finish_reason"] = "length"
        out, notes = collect(frames)
        self.assertEqual(notes, [])
        self.assertEqual(sent_calls(out)[0]["function"]["arguments"], CUT)

    def test_long_held_call_keeps_the_stream_alive(self) -> None:
        with patch.object(call_repair, "KEEPALIVE_S", 0.0):
            out, _ = collect(call_frames(VALID))
        empty = [
            frame
            for frame in out
            if frame.get("choices")
            and frame["choices"][0]["delta"] == {}
            and frame["choices"][0]["finish_reason"] is None
        ]
        self.assertGreaterEqual(len(empty), 1)
        self.assertEqual(len(sent_calls(out)), 1)

    def test_corrected_plan_adds_the_note_on_every_target(self) -> None:
        fallback = Plan(
            json.dumps({"model": "b", "messages": [{"role": "user", "content": "hi"}]}),
            "https://example.invalid/v1",
            10,
            Decimal(1),
            Decimal(1),
        )
        plan = Plan(
            json.dumps({"model": "a", "messages": [{"role": "user", "content": "hi"}]}),
            "https://example.invalid/v1",
            10,
            Decimal(1),
            Decimal(1),
            fallback=fallback,
        )
        fixed = corrected(plan, "note")
        for target in (fixed, fixed.fallback):
            assert target is not None
            body = json.loads(target.body)
            self.assertEqual(body["messages"][-1], {"role": "user", "content": "note"})
            self.assertEqual(len(body["messages"]), 2)
        self.assertEqual(fixed.reserved, plan.reserved)


class GatewayTests(unittest.TestCase):
    """The three public formats through the key edge, with a recorded malformed reply."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = [1800000000.0]
        self.store = Store(
            self.root / "keys.sqlite", "fictional-pepper-" * 4, lambda: self.now[0]
        )
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ATLAS_DATA_DIR": str(self.root),
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_PROJECT_DSN": "",
                    "ATLAS_USER_DAILY_EUR": "10",
                    "ATLAS_GLOBAL_DAILY_EUR": "20",
                    "ATLAS_API_DEFAULT_RAW_MODEL": "",
                },
            )
        )
        self.enterContext(
            patch(
                "services.orchestrator.api_key_edge.configured_store",
                return_value=self.store,
            )
        )
        self.row, self.key = self.store.create(
            org_id="fictional-org",
            member_id="fictional-member",
            subject="tester@example.invalid",
            budget_user="fictional-user",
            name="office-suite",
            created_by="operator",
            daily_requests=50,
        )
        self.client = self.enterContext(TestClient(app))
        self.replies: list[list[dict[str, Any]]] = []
        self.plans: list[Plan] = []
        self.enterContext(
            patch(
                "services.orchestrator.dev_inference.gateway.prepare_api",
                side_effect=lambda *a, **k: Plan(
                    "{}",
                    "https://example.invalid/v1",
                    100000,
                    Decimal(1),
                    Decimal(1),
                    provider="fictional",
                    model="fictional-model",
                ),
            )
        )
        self.enterContext(
            patch(
                "services.orchestrator.dev_inference.gateway.events",
                side_effect=self.frames,
            )
        )

    async def frames(self, plan: Plan) -> AsyncIterator[dict[str, Any]]:
        self.plans.append(plan)
        for frame in self.replies.pop(0):
            yield json.loads(json.dumps(frame))

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self.client.post(
            "/api/atlas/v1" + path,
            headers={"Authorization": "Bearer " + self.key},
            json=body,
        )

    def chat_body(self, **extra: Any) -> dict[str, Any]:
        return {
            "model": "atlas-dev",
            "messages": [{"role": "user", "content": "Put the total in B2"}],
            "tools": TOOLS,
        } | extra

    def test_chat_completions_returns_valid_arguments_after_one_retry(self) -> None:
        self.replies = [call_frames(CUT), call_frames(VALID, "call-2")]
        reply = self.post("/chat/completions", self.chat_body())
        self.assertEqual(reply.status_code, 200, reply.text)
        call = reply.json()["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(json.loads(call["function"]["arguments"]), json.loads(VALID))
        self.assertEqual(reply.json()["usage"]["prompt_tokens"], 200)
        self.assertEqual(len(self.plans), 2)
        note = json.loads(self.plans[1].body)["messages"][-1]
        self.assertEqual(note["role"], "user")
        self.assertIn("write_cell", note["content"])
        self.assertEqual(self.store.usage(self.row["id"])["charged_micro_eur"], 60)

    def test_chat_completions_stream_never_carries_invalid_arguments(self) -> None:
        self.replies = [call_frames(CUT), call_frames(VALID, "call-2")]
        reply = self.post("/chat/completions", self.chat_body(stream=True))
        self.assertEqual(reply.status_code, 200, reply.text)
        arguments = ""
        for line in reply.text.splitlines():
            if line.startswith("data: {"):
                for choice in json.loads(line[6:]).get("choices") or []:
                    for call in choice["delta"].get("tool_calls") or []:
                        arguments += call["function"].get("arguments") or ""
        self.assertEqual(json.loads(arguments), json.loads(VALID))

    def test_two_invalid_attempts_end_in_a_named_error(self) -> None:
        self.replies = [call_frames(CUT), call_frames(CUT, "call-2")]
        reply = self.post("/chat/completions", self.chat_body())
        self.assertEqual(reply.status_code, 502, reply.text)
        self.assertEqual(reply.json()["error"]["type"], "invalid_tool_arguments")
        self.assertEqual(self.store.usage(self.row["id"])["charged_micro_eur"], 60)
        self.replies = [call_frames(CUT), call_frames(CUT, "call-2")]
        reply = self.post("/chat/completions", self.chat_body(stream=True))
        self.assertIn('"invalid_tool_arguments"', reply.text)
        self.assertNotIn("Quarterly total", reply.text)

    def test_messages_tool_use_input_is_an_object(self) -> None:
        self.replies = [call_frames(CUT), call_frames(VALID, "call-2")]
        reply = self.post(
            "/messages",
            {
                "model": "atlas-dev",
                "max_tokens": 256,
                "messages": [{"role": "user", "content": "Put the total in B2"}],
                "tools": [{"name": "write_cell", "input_schema": WRITE}],
            },
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        blocks = [b for b in reply.json()["content"] if b["type"] == "tool_use"]
        self.assertEqual(blocks[0]["input"], json.loads(VALID))

    def test_responses_function_call_arguments_are_valid(self) -> None:
        self.replies = [call_frames(CUT), call_frames(VALID, "call-2")]
        reply = self.post(
            "/responses",
            {
                "model": "atlas-dev",
                "input": "Put the total in B2",
                "tools": [
                    {"type": "function", "name": "write_cell", "parameters": WRITE}
                ],
            },
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        items = [i for i in reply.json()["output"] if i["type"] == "function_call"]
        self.assertEqual(json.loads(items[0]["arguments"]), json.loads(VALID))
        self.replies = [call_frames(CUT), call_frames(CUT, "call-2")]
        reply = self.post(
            "/responses",
            {
                "model": "atlas-dev",
                "input": "Put the total in B2",
                "stream": True,
                "tools": [
                    {"type": "function", "name": "write_cell", "parameters": WRITE}
                ],
            },
        )
        self.assertIn("invalid_tool_arguments", reply.text)
        self.assertIn("response.failed", reply.text)


if __name__ == "__main__":
    unittest.main()
