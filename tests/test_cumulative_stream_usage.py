"""Scripted vLLM cumulative usage, including empty first and final chunks."""

from collections.abc import AsyncIterator
import json
import os
import unittest
from unittest.mock import patch

from agent_provider import AgentRequest
from serverless_support import environment
from stream_transport import attempt
from streaming import StreamDecoder
from test_stream_transport import Content, Session


def chunk(
    content: str,
    prompt: int,
    output: int,
    *,
    finish: bool = False,
    response_id: str = "response-a",
) -> bytes:
    return (
        "data: "
        + json.dumps(
            {
                "id": response_id,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": content},
                        "finish_reason": "stop" if finish else None,
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt,
                    "completion_tokens": output,
                    "total_tokens": prompt + output,
                },
            }
        )
        + "\n\n"
    ).encode()


def cumulative_stream() -> list[bytes]:
    return [
        chunk("", 10, 0),
        chunk("Hello", 10, 1),
        chunk(" world", 10, 2),
        chunk("", 10, 2, finish=True),
        b"data: [DONE]\n\n",
    ]


class CumulativeUsageTests(unittest.TestCase):
    def test_every_chunk_and_fragmented_bytes_keep_last_usage(self) -> None:
        for fragmented in (False, True):
            decoder = StreamDecoder()
            events = []
            for piece in cumulative_stream():
                for fragment in [bytes([b]) for b in piece] if fragmented else [piece]:
                    events.extend(decoder.feed(fragment))
            decoder.finish()
            self.assertEqual(
                events,
                [{"delta": {"content": "Hello"}}, {"delta": {"content": " world"}}],
            )
            self.assertEqual(
                decoder.result,
                {
                    "text": "Hello world",
                    "calls": [],
                    "provider_id": "response-a",
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                },
            )

    def test_decreasing_counts_are_rejected(self) -> None:
        for prompt, output in ((9, 2), (10, 0)):
            decoder = StreamDecoder()
            decoder.feed(chunk("Hello", 10, 1))
            with self.assertRaisesRegex(ValueError, "decreasing_usage"):
                decoder.feed(chunk(" world", prompt, output))

    def test_different_response_id_is_rejected(self) -> None:
        decoder = StreamDecoder()
        decoder.feed(chunk("Hello", 10, 1))
        with self.assertRaisesRegex(ValueError, "provider_id_changed"):
            decoder.feed(chunk(" world", 10, 2, response_id="response-b"))

    def test_usage_only_terminal_chunk_keeps_final_counts(self) -> None:
        decoder = StreamDecoder()
        decoder.feed(chunk("Hello", 10, 1, finish=True))
        decoder.feed(
            b'data: {"id":"response-a","choices":[],"usage":'
            b'{"prompt_tokens":11,"completion_tokens":2}}\n\n'
        )
        decoder.feed(b"data: [DONE]\n\n")
        decoder.finish()
        assert decoder.result is not None
        self.assertEqual(
            decoder.result["usage"], {"prompt_tokens": 11, "completion_tokens": 2}
        )


class CumulativeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_delivers_deltas_and_last_usage(self) -> None:
        class ScriptedContent(Content):
            async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
                for piece in cumulative_stream():
                    yield piece

        session = Session()
        session.response.content = ScriptedContent()
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.aiohttp.ClientSession", return_value=session),
        ):
            events = [
                event
                async for event in attempt(
                    AgentRequest(
                        messages=[{"role": "user", "content": "Hello"}],
                        tools=[],
                        timeout=5.0,
                    ),
                    "test-model",
                    5.0,
                )
            ]
        self.assertEqual(len(events), 3)
        assert isinstance(events[-1]["result"], dict)
        self.assertEqual(
            events[-1]["result"]["usage"], {"prompt_tokens": 10, "completion_tokens": 2}
        )
        self.assertTrue(session.response.content.closed)


class SetupStreamTests(unittest.TestCase):
    def probe(self, body: bytes) -> str:
        from contextlib import redirect_stdout
        import io
        from scripts.atlas_setup import check_connection

        output = io.StringIO()
        with (
            patch("scripts.atlas_setup.build_opener") as opener,
            redirect_stdout(output),
        ):
            opener.return_value.open.side_effect = [
                io.BytesIO(b'{"data":[{"id":"test-model"}]}'),
                io.BytesIO(body),
            ]
            check_connection(
                "https://example.org/v1", "private-test-key", "test-model", "hetzner"
            )
            request = opener.return_value.open.call_args_list[-1].args[0]
            sent = json.loads(request.data)
            self.assertTrue(sent["stream"])
            self.assertEqual(sent["stream_options"], {"include_usage": True})
            self.assertEqual(sent["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("private-test-key", output.getvalue())
        return output.getvalue()

    def test_setup_verifies_cumulative_stream(self) -> None:
        self.assertIn(
            "tiny streaming completion verified",
            self.probe(b"".join(cumulative_stream())),
        )

    def test_setup_rejects_invalid_stream_with_clear_safe_message(self) -> None:
        good = b"".join(cumulative_stream())
        for body in (
            chunk("Hello", 10, 2)
            + chunk(" world", 10, 1, finish=True)
            + b"data: [DONE]\n\n",
            chunk("Hello", 10, 1)
            + chunk(" world", 10, 2, finish=True, response_id="changed"),
            good.removesuffix(b"data: [DONE]\n\n"),
            chunk("Hello", 10, 1) + b"data: [DONE]\n\n",
            good + chunk("trailing", 10, 2),
            b'data: {"error":{"message":"private-test-key"}}\n\n',
            b'{"choices":[{"message":{"content":"not streaming"}}]}',
            b"data: {broken private-test-key}\n\n",
        ):
            with (
                self.subTest(body=body),
                self.assertRaisesRegex(
                    ValueError,
                    "^Provider streaming check failed: invalid or incomplete stream; "
                    "check streaming support, response IDs and cumulative token usage\\.$",
                ),
            ):
                self.probe(body)

    def test_invalid_intermediate_usage_cannot_be_hidden_by_final_usage(self) -> None:
        for invalid in (-1, True, "0", None):
            piece = chunk("", 10, 0).replace(
                b'"completion_tokens": 0',
                b'"completion_tokens": ' + json.dumps(invalid).encode(),
            )
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                StreamDecoder().feed(piece + b"".join(cumulative_stream()[1:]))
            with (
                self.subTest(setup=invalid),
                self.assertRaisesRegex(ValueError, "streaming check failed"),
            ):
                self.probe(piece + b"".join(cumulative_stream()[1:]))
