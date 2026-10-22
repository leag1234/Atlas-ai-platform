"""Text/function compatibility, fragmentation, provider quirks and invalid output."""

import json
from typing import Any
from pathlib import Path
import unittest

from services.orchestrator.dev_chat import ChatInput, Output

TOOL = {
    "type": "function",
    "function": {
        "name": "save_value",
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    },
}
BODY = {
    "model": "atlas-code",
    "messages": [{"role": "user", "content": "Save 7."}],
    "tools": [TOOL],
}


class DevChatTests(unittest.TestCase):
    def test_input_text_parts_and_options(self) -> None:
        request = ChatInput.model_validate(
            BODY
            | {
                "messages": [
                    {
                        "role": "developer",
                        "content": [{"type": "text", "text": "écris"}],
                    }
                ],
                "max_completion_tokens": 64,
            }
        )
        self.assertEqual(request.messages[0].role, "system")
        self.assertEqual(request.messages[0].content, "écris")
        self.assertEqual(request.max_tokens, 64)
        extra: dict[str, Any]
        for extra in (
            {"max_tokens": 1, "max_completion_tokens": 2},
            {"stream": "yes"},
            {"model": "other"},
            {"response_format": {}},
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {}}],
                    }
                ]
            },
        ):
            with self.assertRaises(ValueError):
                ChatInput.model_validate(BODY | extra)

    def test_recorded_fragments_and_named_stop_normalization(self) -> None:
        recordings = json.loads(
            (Path(__file__).parent / "fixtures/devapi-streams.json").read_text()
        )
        for raw in recordings.values():
            output = Output("test", ChatInput.model_validate(BODY))
            arguments = ""
            for line in raw.splitlines():
                if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                    continue
                frame = json.loads(line[5:])
                if frame.get("usage") is None:
                    frame.pop("usage", None)
                chunk = output.feed(frame)
                for choice in chunk["choices"]:
                    for call in choice["delta"].get("tool_calls", []):
                        arguments += call["function"].get("arguments", "")
            self.assertEqual(json.loads(arguments), {"value": 7})
            final = output.result()
            self.assertEqual(final["choices"][0]["finish_reason"], "tool_calls")
            self.assertEqual(
                final["choices"][0]["message"]["tool_calls"][0]["function"]["name"],
                "save_value",
            )
            self.assertEqual(final["usage"]["prompt_tokens"], 166)

    def test_text_truncation_and_adversarial_deltas(self) -> None:
        for reason in ("stop", "length"):
            output = Output("test", ChatInput.model_validate(BODY))
            chunk = output.feed(
                {"choices": [{"delta": {"content": "héllo"}, "finish_reason": reason}]}
            )
            self.assertEqual(chunk["choices"][0]["finish_reason"], reason)
            output.feed(
                {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}
            )
            self.assertEqual(
                output.result()["choices"][0]["message"]["content"], "héllo"
            )
        for delta in (
            {"content": 9},
            {"role": "user"},
            {"content": "x" * 131073},
            {"tool_calls": [{"index": -1}]},
        ):
            output = Output("test", ChatInput.model_validate(BODY))
            with self.assertRaises(ValueError):
                output.feed({"choices": [{"delta": delta, "finish_reason": None}]})
        with self.assertRaises(ValueError):
            Output("test", ChatInput.model_validate(BODY)).result()

    def test_forced_function_cannot_silently_become_text(self) -> None:
        request = ChatInput.model_validate(
            BODY
            | {"tool_choice": {"type": "function", "function": {"name": "save_value"}}}
        )
        with self.assertRaises(ValueError):
            Output("test", request).feed(
                {
                    "choices": [
                        {"delta": {"content": "ignored"}, "finish_reason": "stop"}
                    ]
                }
            )
