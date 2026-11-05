"""Answer-only streams must not expose provider pseudo tool calls."""

import unittest

from packages.limits import MalformedToolCall
from streaming import StreamDecoder

from test_streaming_decoder import end, frame


class AnswerOnlyCallTests(unittest.TestCase):
    def test_ignore_calls_discards_pseudo_call_and_keeps_text(self) -> None:
        decoder = StreamDecoder(ignore_calls=True)
        events = decoder.feed(
            frame({"content": "Useful answer."})
            + frame(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-pseudo",
                            "type": "function",
                            "function": {
                                "name": "rag_search(query=" + "x" * 3000,
                                "arguments": "{}",
                            },
                        }
                    ]
                }
            )
            + end("tool_calls")
        )
        decoder.finish()

        self.assertEqual(events, [{"delta": {"content": "Useful answer."}}])
        assert decoder.result is not None
        self.assertEqual(decoder.result["text"], "Useful answer.")
        self.assertEqual(decoder.result["calls"], [])
        self.assertEqual(decoder.reason, "stop")

    def test_default_decoder_still_rejects_long_call_name(self) -> None:
        decoder = StreamDecoder()
        with self.assertRaises(MalformedToolCall) as context:
            decoder.feed(
                frame(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-pseudo",
                                "type": "function",
                                "function": {
                                    "name": "rag_search(query=" + "x" * 3000,
                                    "arguments": "{}",
                                },
                            }
                        ]
                    }
                )
            )

        self.assertIsInstance(context.exception, MalformedToolCall)

    def test_default_decoder_rejects_long_name_streamed_in_pieces(self) -> None:
        decoder = StreamDecoder()
        name_piece = "x" * 30
        with self.assertRaises(MalformedToolCall) as context:
            decoder.feed(
                frame(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-pseudo",
                                "type": "function",
                                "function": {"name": name_piece},
                            }
                        ]
                    }
                )
                + frame(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "type": "function",
                                "function": {"name": name_piece},
                            }
                        ]
                    }
                )
                + frame(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "type": "function",
                                "function": {"name": name_piece},
                            }
                        ]
                    }
                )
            )

        self.assertEqual(context.exception.measured, 90)

    def test_ignore_calls_accepts_long_name_streamed_in_pieces(self) -> None:
        decoder = StreamDecoder(ignore_calls=True)
        name_piece = "x" * 30
        events = decoder.feed(
            frame({"content": "Useful answer."})
            + frame(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-pseudo",
                            "type": "function",
                            "function": {"name": name_piece},
                        }
                    ]
                }
            )
            + frame(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "type": "function",
                            "function": {"name": name_piece},
                        }
                    ]
                }
            )
            + frame(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "type": "function",
                            "function": {"name": name_piece},
                        }
                    ]
                }
            )
            + end("stop")
        )
        decoder.finish()

        self.assertEqual(events, [{"delta": {"content": "Useful answer."}}])
        assert decoder.result is not None
        self.assertEqual(decoder.result["text"], "Useful answer.")
        self.assertEqual(decoder.result["calls"], [])


if __name__ == "__main__":
    unittest.main()
