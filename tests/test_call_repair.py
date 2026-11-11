"""Unambiguous tool-call slips are repaired before the client sees them."""

import asyncio
import json
import unittest
from collections.abc import AsyncIterator
from typing import Any

from services.orchestrator.call_repair import repair, repaired_frames

EXEC = {
    "type": "object",
    "properties": {
        "cmd": {"type": "string"},
        "workdir": {"type": "string"},
        "yield_time_ms": {"type": "integer"},
    },
    "required": ["cmd"],
}
PATCH = {
    "type": "object",
    "properties": {"input": {"type": "string"}},
    "required": ["input"],
}
DECLARED = {"exec_command": EXEC, "apply_patch": PATCH}


def tools(*names: str) -> list[dict[str, Any]]:
    schemas = {"exec_command": EXEC, "apply_patch": PATCH, "shell": EXEC}
    return [
        {"type": "function", "function": {"name": n, "parameters": schemas[n]}}
        for n in names
    ]


async def collect(
    frames: list[dict[str, Any]], declared: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    async def source() -> AsyncIterator[dict[str, Any]]:
        for frame in frames:
            yield frame

    return [frame async for frame in repaired_frames(source(), declared)]


def call_frame(
    index: int,
    *,
    id: str = "",
    name: str = "",
    arguments: str = "",
    finish: str | None = None,
) -> dict[str, Any]:
    function: dict[str, Any] = {"arguments": arguments}
    if name:
        function["name"] = name
    call: dict[str, Any] = {"index": index, "function": function}
    if id:
        call["id"] = id
    return {
        "choices": [
            {"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": finish}
        ]
    }


def fixed(name: str, arguments: str, declared: dict[str, Any]) -> tuple[str, str, str]:
    result = repair(name, arguments, declared)
    assert result is not None
    return result


class RepairRuleTests(unittest.TestCase):
    def test_undeclared_shell_name_goes_to_the_declared_shell_tool(self) -> None:
        name, arguments, reason = fixed(
            "bash", json.dumps({"command": "ls -la", "cwd": "/work"}), DECLARED
        )
        self.assertEqual((name, reason), ("exec_command", "tool_name"))
        self.assertEqual(json.loads(arguments), {"cmd": "ls -la", "workdir": "/work"})

    def test_argv_form_is_one_command_line(self) -> None:
        _, arguments, _ = fixed(
            "exec", json.dumps({"command": ["bash", "-lc", "rg foo"]}), DECLARED
        )
        self.assertEqual(json.loads(arguments), {"cmd": "rg foo"})

    def test_declared_tool_with_the_wrong_argument_name(self) -> None:
        name, arguments, reason = fixed(
            "exec_command",
            json.dumps({"command": "pytest -q", "yield_time_ms": 1000}),
            DECLARED,
        )
        self.assertEqual((name, reason), ("exec_command", "argument_name"))
        self.assertEqual(
            json.loads(arguments), {"cmd": "pytest -q", "yield_time_ms": 1000}
        )

    def test_correct_and_ambiguous_calls_are_left_alone(self) -> None:
        self.assertIsNone(repair("exec_command", json.dumps({"cmd": "ls"}), DECLARED))
        self.assertIsNone(repair("apply_patch", json.dumps({"patch": "x"}), DECLARED))
        self.assertIsNone(repair("view_image", json.dumps({"path": "a.png"}), DECLARED))
        two_shells = {"exec_command": EXEC, "shell": EXEC}
        self.assertIsNone(repair("bash", json.dumps({"command": "ls"}), two_shells))
        self.assertIsNone(repair("bash", json.dumps({"query": "x"}), DECLARED))


class StreamTests(unittest.TestCase):
    def test_split_call_is_reassembled_and_repaired_text_passes(self) -> None:
        frames = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Checking."},
                        "finish_reason": None,
                    }
                ]
            },
            call_frame(0, id="c1", name="bash", arguments='{"comm'),
            call_frame(0, arguments='and": "git status"}'),
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        ]
        out = asyncio.run(collect(frames, tools("exec_command", "apply_patch")))
        self.assertEqual(out[0]["choices"][0]["delta"], {"content": "Checking."})
        calls = [
            f
            for f in out
            if f.get("choices") and f["choices"][0]["delta"].get("tool_calls")
        ]
        self.assertEqual(len(calls), 1)
        call = calls[0]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual((call["id"], call["function"]["name"]), ("c1", "exec_command"))
        self.assertEqual(
            json.loads(call["function"]["arguments"]), {"cmd": "git status"}
        )
        self.assertEqual(out[-2]["choices"][0]["finish_reason"], "tool_calls")
        self.assertIn("usage", out[-1])

    def test_two_calls_keep_their_order_and_good_calls_are_unchanged(self) -> None:
        frames = [
            call_frame(0, id="a", name="exec_command", arguments='{"cmd": "ls"}'),
            call_frame(
                0, id="b", name="apply_patch", arguments='{"input": "*** Begin Patch"}'
            ),
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]
        out = asyncio.run(collect(frames, tools("exec_command", "apply_patch")))
        calls = out[0]["choices"][0]["delta"]["tool_calls"]
        self.assertEqual([c["id"] for c in calls], ["a", "b"])
        # Provider indices are kept: the client opens a new call on a new id.
        self.assertEqual([c["index"] for c in calls], [0, 0])
        self.assertEqual(
            calls[1]["function"]["arguments"], '{"input": "*** Begin Patch"}'
        )

    def test_without_declared_tools_frames_are_untouched(self) -> None:
        frames = [call_frame(0, id="a", name="bash", arguments="{}")]
        self.assertEqual(asyncio.run(collect(frames, [])), frames)


if __name__ == "__main__":
    unittest.main()


class StreamingKeptTests(unittest.TestCase):
    def test_other_calls_still_stream_their_arguments(self) -> None:
        frames = [
            call_frame(0, id="w", name="get_weather", arguments='{"ci'),
            call_frame(0, arguments='ty": "Lyon"}'),
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]
        declared = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                },
            }
        ]
        out = asyncio.run(collect(frames, declared))
        pieces = [
            f["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
            for f in out
            if f["choices"] and f["choices"][0]["delta"].get("tool_calls")
        ]
        self.assertEqual(pieces, ['{"ci', 'ty": "Lyon"}'])
