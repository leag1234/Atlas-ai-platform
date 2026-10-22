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
