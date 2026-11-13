"""A long tool call under way is signalled while written, so the gateway does not cut it as silent."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "services" / "model-gateway")
)

from streaming import (  # noqa: E402
    TOOL_PROGRESS_CHARS,
    TOOL_PROGRESS_STEP,
    StreamDecoder,
)


def frame(arguments: str, *, first: bool = False) -> bytes:
    call: dict[str, object] = {"index": 0, "function": {"arguments": arguments}}
    if first:
        call["id"] = "call-1"
        call["function"] = {"name": "publish_document", "arguments": arguments}
    payload = {
        "id": "response-a",
        "choices": [{"index": 0, "delta": {"tool_calls": [call]}}],
    }
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


class ToolProgressTests(unittest.TestCase):
    def test_a_long_call_is_signalled_while_it_is_written(self) -> None:
        decoder = StreamDecoder()
        events = decoder.feed(frame("x" * 100, first=True))
        self.assertEqual(events, [{"tool_call": "publish_document"}])
        events = decoder.feed(frame("y" * TOOL_PROGRESS_CHARS))
        self.assertEqual(events, [{"tool_progress": TOOL_PROGRESS_CHARS + 100}])
        # Less than a step later: nothing; a whole step later: a new signal, so
        # a call still being written is never taken for a silent one.
        self.assertEqual(decoder.feed(frame("z" * (TOOL_PROGRESS_STEP - 1))), [])
        self.assertEqual(
            decoder.feed(frame("z")),
            [{"tool_progress": TOOL_PROGRESS_CHARS + 100 + TOOL_PROGRESS_STEP}],
        )

    def test_a_short_call_is_announced_but_not_signalled_as_long(self) -> None:
        decoder = StreamDecoder()
        self.assertEqual(
            decoder.feed(frame('{"query": "x"}', first=True)),
            [{"tool_call": "publish_document"}],
        )
        self.assertEqual(decoder.feed(frame('"}')), [])


class ToolCallStartTests(unittest.TestCase):
    """Staging provider, 2026-11-13: a document's arguments came in one frame after 110 s."""

    def test_a_call_is_announced_once_with_its_first_arguments(self) -> None:
        decoder = StreamDecoder()
        # The head (name, no arguments yet) says nothing; the first argument
        # characters announce the call; the held-back value then adds nothing
        # until it arrives whole.
        self.assertEqual(decoder.feed(frame("", first=True)), [])
        self.assertEqual(
            decoder.feed(frame('{"format": "pdf"')),
            [{"tool_call": "publish_document"}],
        )
        self.assertEqual(decoder.feed(frame(', "path": "a.pdf"')), [])

    def test_each_call_is_announced(self) -> None:
        decoder = StreamDecoder()
        decoder.feed(frame("{", first=True))
        payload = {
            "id": "response-a",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call-2",
                                "function": {"name": "web_search", "arguments": "{"},
                            }
                        ]
                    },
                }
            ],
        }
        self.assertEqual(
            decoder.feed(b"data: " + json.dumps(payload).encode() + b"\n\n"),
            [{"tool_call": "web_search"}],
        )

    def test_ignored_calls_are_not_announced(self) -> None:
        decoder = StreamDecoder(ignore_calls=True)
        self.assertEqual(decoder.feed(frame("{", first=True)), [])


if __name__ == "__main__":
    unittest.main()
