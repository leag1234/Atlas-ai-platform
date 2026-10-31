"""Server-side trimming of old tool outputs (fictional data only)."""

import unittest

from services.orchestrator.context_trim import BLOCK, KEEP, trim_messages, trimmed_count
from packages.dev_request import Call
from services.orchestrator.dev_chat import RawMessage


def history(tool_outputs: int) -> list[RawMessage]:
    messages = [RawMessage(role="user", content="Fix the fictional parser.")]
    for index in range(tool_outputs):
        messages.append(
            RawMessage(
                role="assistant",
                tool_calls=[
                    Call.model_validate(
                        {
                            "id": f"c{index}",
                            "type": "function",
                            "function": {"name": "exec_command", "arguments": "{}"},
                        }
                    )
                ],
            )
        )
        messages.append(
            RawMessage(
                role="tool", tool_call_id=f"c{index}", content=f"output {index} " * 50
            )
        )
    return messages


class ContextTrimTests(unittest.TestCase):
    def test_short_histories_are_untouched(self) -> None:
        messages = history(KEEP + BLOCK - 1)
        self.assertIs(trim_messages(messages), messages)

    def test_oldest_outputs_are_stubbed_in_whole_blocks(self) -> None:
        trimmed = trim_messages(history(KEEP + BLOCK + 5))
        tools = [m for m in trimmed if m.role == "tool"]
        stubbed = [m for m in tools if str(m.content).startswith("[Atlas removed")]
        self.assertEqual(len(stubbed), BLOCK)
        self.assertTrue(all("characters" in str(m.content) for m in stubbed))
        self.assertEqual(tools[-1].content, f"output {KEEP + BLOCK + 4} " * 50)
        self.assertEqual(tools[BLOCK].tool_call_id, f"c{BLOCK}")
        self.assertFalse(str(tools[BLOCK].content).startswith("[Atlas removed"))

    def test_prefix_is_stable_between_two_trimming_points(self) -> None:
        first = trim_messages(history(KEEP + BLOCK + 1))
        later = trim_messages(history(KEEP + 2 * BLOCK - 1))
        self.assertEqual(
            trimmed_count(KEEP + BLOCK + 1), trimmed_count(KEEP + 2 * BLOCK - 1)
        )
        self.assertEqual(first, later[: len(first)])

    def test_tool_call_structure_is_kept(self) -> None:
        trimmed = trim_messages(history(KEEP + BLOCK))
        self.assertEqual(
            [m.role for m in trimmed], [m.role for m in history(KEEP + BLOCK)]
        )
        self.assertEqual(
            [m.tool_call_id for m in trimmed],
            [m.tool_call_id for m in history(KEEP + BLOCK)],
        )


if __name__ == "__main__":
    unittest.main()
