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
        self.assertEqual(events, [])
        events = decoder.feed(frame("y" * TOOL_PROGRESS_CHARS))
        self.assertEqual(events, [{"tool_progress": TOOL_PROGRESS_CHARS + 100}])
        # Less than a step later: nothing; a whole step later: a new signal, so
        # a call still being written is never taken for a silent one.
        self.assertEqual(decoder.feed(frame("z" * (TOOL_PROGRESS_STEP - 1))), [])
        self.assertEqual(
            decoder.feed(frame("z")),
            [{"tool_progress": TOOL_PROGRESS_CHARS + 100 + TOOL_PROGRESS_STEP}],
        )

    def test_a_short_call_is_not_signalled(self) -> None:
        decoder = StreamDecoder()
        self.assertEqual(decoder.feed(frame('{"query": "x"}', first=True)), [])


if __name__ == "__main__":
    unittest.main()
