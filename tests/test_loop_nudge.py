"""Loop nudge: a note on the newest tool output only when identical calls keep returning the same result."""

import json
import unittest

from packages.dev_request import Message
from services.orchestrator.loop_nudge import NOTE, nudge_messages


def turn(call_id: str, command: str, output: str) -> list[Message]:
    call = {
        "id": call_id,
        "type": "function",
        "function": {"name": "exec_command", "arguments": json.dumps({"cmd": command})},
    }
    return [
        Message.model_validate({"role": "assistant", "tool_calls": [call]}),
        Message(role="tool", tool_call_id=call_id, content=output),
    ]


def history(*turns: tuple[str, str]) -> list[Message]:
    messages = [Message(role="user", content="Add a badge next to the logo.")]
    for number, (command, output) in enumerate(turns):
        messages += turn(f"call_{number}", command, output)
    return messages


EMPTY = "Chunk ID: {}\nWall time: 0.00{} seconds\nProcess exited with code 0\nOutput:\n"


class LoopNudgeTests(unittest.TestCase):
    def test_same_call_and_same_result_three_times_gets_one_note(self) -> None:
        messages = history(
            *[("rg -n beta reports", EMPTY.format(chunk, chunk)) for chunk in "123"]
        )
        nudged, repeats = nudge_messages(messages)
        self.assertEqual(repeats, 3)
        self.assertTrue(
            nudged[-1].content.endswith(NOTE.format(name="exec_command", count=3))
        )
        self.assertEqual(nudged[:-1], messages[:-1])
        self.assertNotIn("Atlas note", messages[-1].content)

    def test_alternating_loop_is_detected(self) -> None:
        turns = [
            ("rg a reports", EMPTY.format(i, i))
            if i % 2
            else ("rg a BRAIN", EMPTY.format(i, i))
            for i in range(6)
        ]
        _, repeats = nudge_messages(history(*turns))
        self.assertEqual(repeats, 3)

    def test_rerun_with_a_different_result_is_left_alone(self) -> None:
        messages = history(
            ("make test", "3 failed"),
            ("make test", "2 failed"),
            ("make test", "1 failed"),
        )
        self.assertEqual(nudge_messages(messages), (messages, 0))

    def test_two_repeats_are_not_a_loop(self) -> None:
        messages = history(("ls", "a b"), ("ls", "a b"))
        self.assertEqual(nudge_messages(messages), (messages, 0))

    def test_older_repeats_outside_the_window_do_not_count(self) -> None:
        turns = (
            [("ls", "a b")] * 2
            + [(f"cat {i}", str(i)) for i in range(12)]
            + [("ls", "a b")]
        )
        messages = history(*turns)
        self.assertEqual(nudge_messages(messages), (messages, 0))

    def test_history_without_tool_calls_is_unchanged(self) -> None:
        messages = [Message(role="user", content="Hello")]
        self.assertEqual(nudge_messages(messages), (messages, 0))


if __name__ == "__main__":
    unittest.main()
