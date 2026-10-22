"""Synthetic Scaleway probes recorded on 2026-09-10; no live call in CI."""

import gzip
from pathlib import Path
import unittest

from streaming import StreamDecoder


class RecordedStreamTests(unittest.TestCase):
    def test_recorded_plain_and_reasoning_frames(self) -> None:
        for effort in ("none", "low"):
            with self.subTest(effort=effort):
                data = gzip.decompress(
                    Path(
                        "services/model-gateway/fixtures/" + effort + ".sse.gz"
                    ).read_bytes()
                )
                decoder = StreamDecoder()
                events = []
                for offset in range(0, len(data), 7):
                    events.extend(decoder.feed(data[offset : offset + 7]))
                decoder.finish()
                assert decoder.result is not None
                self.assertTrue(decoder.result["text"])
                self.assertGreater(len(events), 2)
                reasoning = [
                    e
                    for e in events
                    if isinstance(e["delta"], dict)
                    and "reasoning_content" in e["delta"]
                ]
                self.assertEqual(bool(reasoning), effort == "low")
