"""REQ-ENG-004: preserve real Work tool calls with compatible stop framing."""

import json
from pathlib import Path
import unittest

from streaming import StreamDecoder


class HetznerWorkCallsTests(unittest.TestCase):
    def test_recorded_edit_survives_stop_and_fragmented_transport(self) -> None:
        recording = json.loads(
            Path("services/model-gateway/fixtures/hetzner-work-edit.json").read_text()
        )
        raw = recording["sse"].encode()
        for size in (1, 31, len(raw)):
            with self.subTest(chunk_bytes=size):
                decoder = StreamDecoder(allow_length=True)
                for offset in range(0, len(raw), size):
                    decoder.feed(raw[offset : offset + size])
                decoder.finish()
                result = decoder.result
                assert result is not None
                self.assertEqual(result["finish_reason"], "stop")
                self.assertEqual(result["text"], "")
                self.assertEqual(
                    result["calls"],
                    [
                        {
                            "id": "chatcmpl-tool-a0b204c11a359229",
                            "name": "edit_file",
                            "arguments": json.dumps(
                                {
                                    "path": "/workspace/842150d7-59aa-4e6d-bbbf-989c46bd00d6/totals.py",
                                    "old_text": "def total_after_discount(items, discount):\n    return sum(items)",
                                    "new_text": "def total_after_discount(items, discount):\n    total = sum(items)\n    return max(0, total - discount)",
                                }
                            ),
                        }
                    ],
                )
                self.assertEqual(
                    result["usage"],
                    {"prompt_tokens": 6440, "completion_tokens": 126},
                )


if __name__ == "__main__":
    unittest.main()
