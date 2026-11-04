"""Messages native text/tool event lifecycle and correlated tool results."""

import json
import unittest
from typing import Any
from devapi_support import Environment

PAYLOAD: dict[str, Any] = {
    "model": "atlas-code",
    "max_tokens": 512,
    "messages": [{"role": "user", "content": "Save 7."}],
    "tools": [{"name": "save_value", "input_schema": {"type": "object"}}],
}


class DevMessagesTests(unittest.TestCase):
    def test_http_json_and_sse_tool_result(self) -> None:
        env = Environment()
        self.addCleanup(env.close)
        reply = env.client.post("/v1/messages", headers=env.headers, json=PAYLOAD)
        self.assertEqual(reply.status_code, 200, reply.text)
        result = reply.json()
        self.assertEqual(result["type"], "message")
        self.assertEqual(result["stop_reason"], "tool_use")
        block = result["content"][0]
        self.assertEqual(block["input"], {"value": 7})
        self.assertEqual(result["usage"], {"input_tokens": 166, "output_tokens": 7})
        conversation = PAYLOAD["messages"] + [
            {"role": "assistant", "content": result["content"]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": "done",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
        ]
        reply = env.client.post(
            "/v1/messages",
            headers=env.headers,
            json=PAYLOAD | {"messages": conversation, "stream": True},
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        events = [
            json.loads(x[5:]) for x in reply.text.splitlines() if x.startswith("data:")
        ]
        self.assertEqual(events[0]["type"], "message_start")
        self.assertIsNone(events[0]["message"]["stop_reason"])
        self.assertEqual(events[-1]["type"], "message_stop")
        self.assertEqual(events[-2]["delta"]["stop_reason"], "tool_use")
        changes = [
            e["delta"]["partial_json"]
            for e in events
            if e["type"] == "content_block_delta"
        ]
        self.assertGreater(len(changes), 1)
        self.assertEqual(json.loads("".join(changes)), {"value": 7})
        self.assertEqual(sum(e["type"] == "content_block_start" for e in events), 1)
        self.assertEqual(sum(e["type"] == "content_block_stop" for e in events), 1)
        body = json.loads(env.inputs[-1])
        self.assertEqual(body["messages"][-1]["tool_call_id"], block["id"])
        self.assertEqual(env.store.usage("alice")["unknown_micro_eur"], 0)

    def test_unsupported_options_answer_a_named_refusal(self) -> None:
        # Each refusal says what is unsupported, never a bare invalid_request.
        env = Environment()
        self.addCleanup(env.close)
        image = {"type": "image", "source": {"type": "file", "file_id": "f"}}
        for extra, named in (
            ({"thinking": {"type": "enabled", "budget_tokens": 1024}}, "thinking"),
            ({"output_config": {"effort": "max"}}, "effort"),
            ({"tool_choice": {"type": "computer"}}, "tool_choice"),
            ({"messages": [{"role": "user", "content": [image]}]}, "image source"),
        ):
            reply = env.client.post(
                "/v1/messages", headers=env.headers, json=PAYLOAD | extra
            )
            self.assertEqual(reply.status_code, 400, reply.text)
            self.assertIn(named, reply.text)
        self.assertEqual(env.inputs, [])

    def test_thinking_blocks_when_thinking_is_requested(self) -> None:
        frames = [
            {"choices": [{"index": 0, "delta": {"reasoning_content": "Bat is "}}]},
            {"choices": [{"index": 0, "delta": {"reasoning_content": "ball + 1."}}]},
            {"choices": [{"index": 0, "delta": {"content": "0.05"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 20}},
        ]
        raw = (
            "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
            + "data: [DONE]\n\n"
        ).encode()
        body = {
            "model": "atlas-code",
            "max_tokens": 4000,
            "messages": [{"role": "user", "content": "Ball price?"}],
            "thinking": {"type": "enabled", "budget_tokens": 2048},
        }
        env = Environment()
        self.addCleanup(env.close)
        env.raw = raw
        reply = env.client.post("/v1/messages", headers=env.headers, json=body)
        self.assertEqual(reply.status_code, 200, reply.text)
        content = reply.json()["content"]
        self.assertEqual([block["type"] for block in content], ["thinking", "text"])
        self.assertEqual(content[0]["thinking"], "Bat is ball + 1.")
        self.assertTrue(content[0]["signature"])
        self.assertEqual(json.loads(env.inputs[-1])["reasoning_effort"], "low")
        streamed = env.client.post(
            "/v1/messages", headers=env.headers, json=body | {"stream": True}
        )
        events = [
            json.loads(line[5:])
            for line in streamed.text.splitlines()
            if line.startswith("data:")
        ]
        starts = [e for e in events if e["type"] == "content_block_start"]
        self.assertEqual(
            [(e["index"], e["content_block"]["type"]) for e in starts],
            [(0, "thinking"), (1, "text")],
        )
        # Blocks never overlap: thinking is signed and stopped before text starts.
        sequence = [
            (e["type"], e.get("index"), (e.get("delta") or {}).get("type"))
            for e in events
            if e["type"].startswith("content_block")
        ]
        self.assertEqual(
            sequence,
            [
                ("content_block_start", 0, None),
                ("content_block_delta", 0, "thinking_delta"),
                ("content_block_delta", 0, "thinking_delta"),
                ("content_block_delta", 0, "signature_delta"),
                ("content_block_stop", 0, None),
                ("content_block_start", 1, None),
                ("content_block_delta", 1, "text_delta"),
                ("content_block_stop", 1, None),
            ],
        )
        signature = next(
            e["delta"]["signature"]
            for e in events
            if e["type"] == "content_block_delta"
            and e["delta"]["type"] == "signature_delta"
        )
        self.assertEqual(signature, content[0]["signature"])

    def test_text_truncation_and_error_events(self) -> None:
        from services.orchestrator.dev_chat import Output
        from services.orchestrator.dev_input import messages
        from services.orchestrator.dev_messages import Wire

        for reason in ("stop", "length"):
            output = Output("request", messages(PAYLOAD))
            wire = Wire(output, {})
            events = wire.start()
            changes: list[dict[str, Any]] = [{"content": "élève"}]
            if reason == "length":
                changes += [
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call1",
                                "function": {
                                    "name": "save_value",
                                    "arguments": '{"value":7,"unfinished":',
                                },
                            }
                        ]
                    }
                ]
            for delta in changes:
                events += wire.feed(
                    output.feed({"choices": [{"delta": delta, "finish_reason": None}]})
                )
            events += wire.feed(
                output.feed({"choices": [{"delta": {}, "finish_reason": reason}]})
            )
            events += wire.feed(
                output.feed(
                    {
                        "choices": [],
                        "usage": {"prompt_tokens": 2, "completion_tokens": 3},
                    }
                )
            )
            self.assertEqual(wire.result()["content"][0]["text"], "élève")
            self.assertEqual(
                events[-2]["delta"]["stop_reason"],
                "max_tokens" if reason == "length" else "end_turn",
            )
            if reason == "length":
                self.assertEqual(wire.result()["content"][1]["input"], {"value": 7})
            self.assertEqual(
                wire.result()["usage"], {"input_tokens": 2, "output_tokens": 3}
            )
            failure = wire.feed({"error": {"message": "private provider content"}})
            self.assertEqual(
                failure,
                [
                    {
                        "type": "error",
                        "error": {"type": "api_error", "message": "provider_error"},
                    }
                ],
            )

    def test_stop_sequence_reported_when_the_provider_names_it(self) -> None:
        from services.orchestrator.dev_chat import Output
        from services.orchestrator.dev_input import messages
        from services.orchestrator.dev_messages import Wire

        payload = PAYLOAD | {"stop_sequences": ["END", "5"]}
        # vLLM names the sequence; the Mistral API does not (measured 2026-11-03);
        # a sequence the client did not ask for is never reported.
        for provider_says, expected in (("5", "5"), (None, None), ("other", None)):
            output = Output("request", messages(payload))
            wire = Wire(output, {})
            events = wire.start()
            events += wire.feed(
                output.feed(
                    {
                        "choices": [
                            {"delta": {"content": "1,2,3,4,"}, "finish_reason": None}
                        ]
                    }
                )
            )
            events += wire.feed(
                output.feed(
                    {
                        "choices": [
                            {
                                "delta": {},
                                "finish_reason": "stop",
                                "stop_reason": provider_says,
                            }
                        ]
                    }
                )
            )
            events += wire.feed(
                output.feed(
                    {
                        "choices": [],
                        "usage": {"prompt_tokens": 2, "completion_tokens": 3},
                    }
                )
            )
            reason = "stop_sequence" if expected else "end_turn"
            self.assertEqual(wire.result()["stop_reason"], reason)
            self.assertEqual(wire.result()["stop_sequence"], expected)
            delta = events[-2]["delta"]
            self.assertEqual(
                (delta["stop_reason"], delta["stop_sequence"]), (reason, expected)
            )

    def test_malformed_truncated_object_has_generic_error(self) -> None:
        env = Environment()
        self.addCleanup(env.close)
        frames = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call1",
                                    "function": {
                                        "name": "save_value",
                                        "arguments": "[",
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
            {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3}},
        ]
        env.raw = (
            "".join("data: " + json.dumps(f) + "\n\n" for f in frames)
            + "data: [DONE]\n\n"
        ).encode()
        for streaming in (False, True):
            reply = env.client.post(
                "/v1/messages",
                headers=env.headers,
                json=PAYLOAD | {"stream": streaming},
            )
            if streaming:
                self.assertIn('"type": "api_error"', reply.text)
                self.assertNotIn("message_stop", reply.text)
            else:
                self.assertEqual(reply.status_code, 502)
            self.assertNotIn("invalid_tool_input", reply.text)
