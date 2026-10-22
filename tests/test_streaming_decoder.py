"""M11: bounded incremental provider frames, never fabricated chunk splitting."""

import json
import unittest

from streaming import StreamDecoder


def frame(delta: dict[str, object], finish: str | None = None) -> bytes:
    return (
        "data: "
        + json.dumps(
            {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]},
            ensure_ascii=False,
        )
        + "\r\n\r\n"
    ).encode()


def end(finish: str = "stop", output: int = 8) -> bytes:
    usage = {"prompt_tokens": 20, "completion_tokens": output}
    return (
        frame({}, finish)
        + (
            "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"
            "data: [DONE]\n\n"
        ).encode()
    )


class StreamDecoderTests(unittest.TestCase):
    def test_error_body_cannot_be_accepted_alongside_valid_choices(self) -> None:
        payload = {
            "error": {"message": "private provider diagnostic"},
            "choices": [{"index": 0, "delta": {"content": "false success"}}],
        }
        decoder = StreamDecoder(allow_length=True)
        with self.assertRaises(ValueError):
            decoder.feed(("data: " + json.dumps(payload) + "\n\n").encode())

    def test_profile_empty_stop_retains_validated_usage_for_recovery(self) -> None:
        decoder = StreamDecoder(allow_length=True)
        decoder.feed(end(output=1))
        decoder.finish()
        assert decoder.result is not None
        self.assertEqual(decoder.result["text"], "")
        self.assertEqual(
            decoder.result["usage"], {"prompt_tokens": 20, "completion_tokens": 1}
        )
        self.assertEqual(decoder.result["finish_reason"], "stop")
        with self.assertRaisesRegex(ValueError, "empty_stream"):
            StreamDecoder().feed(end(output=1))

    def test_unicode_delta_is_available_before_terminal_usage(self) -> None:
        decoder = StreamDecoder()
        events: list[dict[str, object]] = []
        for byte in frame({"content": "été 漢字 😀"}):
            events.extend(decoder.feed(bytes([byte])))
        self.assertEqual(events, [{"delta": {"content": "été 漢字 😀"}}])
        self.assertIsNone(decoder.result)
        events = decoder.feed(frame({"content": " suite"}) + end())
        self.assertEqual(events[0], {"delta": {"content": " suite"}})
        decoder.finish()
        self.assertEqual(
            decoder.result,
            {
                "text": "été 漢字 😀 suite",
                "calls": [],
                "usage": {"prompt_tokens": 20, "completion_tokens": 8},
            },
        )

    def test_reasoning_normalized_separately_without_inventing_usage(self) -> None:
        decoder = StreamDecoder()
        events = decoder.feed(frame({"reasoning": "calcul synthétique"}))
        self.assertEqual(
            events, [{"delta": {"reasoning_content": "calcul synthétique"}}]
        )
        events = decoder.feed(frame({"reasoning_content": " puis vérification"}))
        self.assertEqual(
            events, [{"delta": {"reasoning_content": " puis vérification"}}]
        )
        decoder.feed(frame({"content": "437"}) + end(output=100))
        decoder.finish()
        self.assertIsNotNone(decoder.result)
        assert decoder.result is not None
        self.assertEqual(decoder.result["text"], "437")
        self.assertEqual(
            decoder.result["usage"], {"prompt_tokens": 20, "completion_tokens": 100}
        )

    def test_fragmented_tool_arguments_are_not_answer_content(self) -> None:
        decoder = StreamDecoder()
        first = frame(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-a",
                        "type": "function",
                        "function": {
                            "name": "calculator",
                            "arguments": '{"expression":"19',
                        },
                    }
                ]
            }
        )
        second = frame(
            {"tool_calls": [{"index": 0, "function": {"arguments": ' * 23"}'}}]}
        )
        events = decoder.feed(first + second + end("tool_calls"))
        decoder.finish()
        self.assertFalse(any("delta" in item and item["delta"] for item in events))
        assert decoder.result is not None
        self.assertEqual(
            decoder.result["calls"],
            [
                {
                    "id": "call-a",
                    "name": "calculator",
                    "arguments": '{"expression":"19 * 23"}',
                }
            ],
        )
        self.assertEqual(decoder.result["text"], "")

    def test_missing_usage_finish_or_done_cannot_be_success(self) -> None:
        for suffix in [
            b"",
            frame({}, "stop"),
            b"data: [DONE]\n\n",
            frame({}, "stop") + b"data: [DONE]\n\n",
        ]:
            with self.subTest(suffix=suffix):
                decoder = StreamDecoder()
                with self.assertRaises(ValueError):
                    decoder.feed(frame({"content": "incomplet"}) + suffix)
                    decoder.finish()

    def test_invalid_output_usage_and_length_finish(self) -> None:
        for suffix in [end("length"), end(output=2049), end(output=-1)]:
            with self.subTest(suffix=suffix):
                decoder = StreamDecoder()
                with self.assertRaises(ValueError):
                    decoder.feed(frame({"content": "texte"}) + suffix)
                    decoder.finish()

    def test_rejects_malformed_frames_and_choice_indices(self) -> None:
        values = [
            b"data: {broken}\n\n",
            b"data: []\n\n",
            b'data: {"choices":[{"index":1,"delta":{"content":"wrong"}}]}\n\n',
            frame({"content": 42}),
            frame({"reasoning": ["invalid"]}),
            frame({"tool_calls": [{"index": 11, "function": {"name": "calculator"}}]}),
        ]
        for value in values:
            with self.subTest(value=value):
                decoder = StreamDecoder()
                with self.assertRaises(ValueError):
                    decoder.feed(value)
                    decoder.finish()

    def test_line_and_total_response_limits(self) -> None:
        decoder = StreamDecoder()
        with self.assertRaises(ValueError):
            decoder.feed(b"data: " + b"x" * 800001)
        decoder = StreamDecoder()
        with self.assertRaises(ValueError):
            for _ in range(1000):
                decoder.feed(frame({"content": "x" * 1000}))

    def test_comments_and_empty_choices_are_not_text(self) -> None:
        decoder = StreamDecoder()
        self.assertEqual(decoder.feed(b": heartbeat\n\n"), [])
        self.assertEqual(decoder.feed(b'data: {"choices":[]}\n\n'), [])
        self.assertEqual(decoder.feed(frame({"role": "assistant", "content": ""})), [])
        decoder.feed(frame({"content": "ok"}) + end())
        decoder.finish()
