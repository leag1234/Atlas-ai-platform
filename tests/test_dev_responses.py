"""Responses lifecycle, incremental function arguments and stateless client results."""

import json
from typing import Any
import unittest
from devapi_support import Environment
from services.orchestrator.dev_input import responses
from services.orchestrator.dev_chat import Output
from services.orchestrator.dev_responses import Wire

PAYLOAD: dict[str, Any] = {
    "model": "atlas-code",
    "input": "Save 7.",
    "store": False,
    "tools": [
        {"type": "function", "name": "save_value", "parameters": {"type": "object"}}
    ],
}


class DevResponsesTests(unittest.TestCase):
    def test_http_json_and_incremental_sse(self) -> None:
        env = Environment()
        self.addCleanup(env.close)
        for streaming in (False, True):
            reply = env.client.post(
                "/v1/responses",
                headers=env.headers,
                json=PAYLOAD | {"stream": streaming},
            )
            self.assertEqual(reply.status_code, 200, reply.text)
            if streaming:
                frames = [
                    json.loads(line[5:])
                    for line in reply.text.splitlines()
                    if line.startswith("data:")
                ]
                self.assertEqual(frames[0]["type"], "response.created")
                self.assertEqual(frames[0]["response"]["output"], [])
                self.assertEqual(frames[-1]["type"], "response.completed")
                self.assertEqual(
                    [e["sequence_number"] for e in frames], list(range(len(frames)))
                )
                self.assertGreater(
                    sum(
                        e["type"] == "response.function_call_arguments.delta"
                        for e in frames
                    ),
                    1,
                )
                result = frames[-1]["response"]
            else:
                result = reply.json()
            self.assertEqual(result["status"], "completed")
            self.assertFalse(result["store"])
            self.assertEqual(json.loads(result["output"][0]["arguments"]), {"value": 7})
            self.assertEqual(result["output"][0]["type"], "function_call")
            self.assertEqual(result["usage"]["input_tokens"], 166)
        self.assertEqual(env.store.usage("alice")["unknown_micro_eur"], 0)

    def test_text_multiple_functions_truncation_and_errors(self) -> None:
        for reason in ("tool_calls", "length"):
            output = Output("test", responses(PAYLOAD))
            wire = Wire(output, PAYLOAD)
            events = wire.start()
            deltas = [
                {"content": "héllo"},
                {
                    "tool_calls": [
                        {
                            "index": i,
                            "id": "call" + str(i),
                            "function": {
                                "name": "save_value",
                                "arguments": '{"value":7}'
                                if reason == "tool_calls"
                                else '{"value":',
                            },
                        }
                        for i in range(2)
                    ]
                },
            ]
            for delta in deltas:
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
            self.assertEqual(len(wire.result()["output"]), 3)
            self.assertEqual(wire.result()["output"][0]["content"][0]["text"], "héllo")
            expected = "completed" if reason == "tool_calls" else "incomplete"
            self.assertEqual(events[-1]["type"], "response." + expected)
            self.assertEqual(wire.result()["status"], expected)
            if reason == "length":
                self.assertEqual(
                    wire.result()["incomplete_details"]["reason"], "max_output_tokens"
                )
            self.assertEqual(
                wire.feed({"error": {"message": "hidden"}})[0]["message"],
                "provider_error",
            )

    def test_client_function_output_item_metadata(self) -> None:
        history = [
            {
                "type": "function_call",
                "call_id": "call1",
                "name": "save_value",
                "arguments": '{"value":7}',
            },
            {
                "type": "function_call_output",
                "id": "item1",
                "call_id": "call1",
                "output": "saved",
            },
        ]
        parsed = responses(PAYLOAD | {"input": history})
        self.assertEqual(parsed.messages[-1].tool_call_id, "call1")
        self.assertNotIn("item1", parsed.model_dump_json())
        history[-1]["id"] = "x" * 1048577
        with self.assertRaises(ValueError):
            responses(PAYLOAD | {"input": history})
