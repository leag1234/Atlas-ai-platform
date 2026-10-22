"""Messages events over the shared validated text/function stream."""

from typing import Any
from pydantic_core import from_json
from services.orchestrator.dev_chat import Output
from services.orchestrator.dev_responses import Wire as ResponsesWire


class Wire:
    def __init__(self, output: Output, request: dict[str, Any]) -> None:
        self.output = output
        self.core = ResponsesWire(output, request)

    def result(self) -> dict[str, Any]:
        content = []
        for item in self.core.items:
            if item["type"] == "message":
                content.append({"type": "text", "text": item["content"][0]["text"]})
            else:
                # A truncated object is explicitly max_tokens, never a completed call.
                args = from_json(
                    item["arguments"] or "{}",
                    allow_inf_nan=False,
                    allow_partial=self.output.reason == "length",
                )
                if not isinstance(args, dict):
                    raise ValueError("invalid_tool_input")
                content.append(
                    {
                        "type": "tool_use",
                        "id": item["call_id"],
                        "name": item["name"],
                        "input": args,
                    }
                )
        usage = self.output.usage or {"prompt_tokens": 0, "completion_tokens": 0}
        return {
            "id": "msg_" + str(self.output.base["id"]),
            "type": "message",
            "role": "assistant",
            "model": "atlas-code",
            "content": content,
            "stop_reason": {
                "stop": "end_turn",
                "tool_calls": "tool_use",
                "length": "max_tokens",
            }.get(self.output.reason or ""),
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage["prompt_tokens"],
                "output_tokens": usage["completion_tokens"],
            },
        }

    def start(self) -> list[dict[str, Any]]:
        return [{"type": "message_start", "message": self.result()}]

    def feed(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for event in self.core.feed(chunk):
            kind = event["type"]
            if kind == "error":
                events.append(
                    {
                        "type": "error",
                        "error": {"type": "api_error", "message": "provider_error"},
                    }
                )
            elif kind == "response.output_item.added":
                item = event["item"]
                block: dict[str, Any] = (
                    {"type": "text", "text": ""}
                    if item["type"] == "message"
                    else {
                        "type": "tool_use",
                        "id": item["call_id"],
                        "name": item["name"],
                        "input": {},
                    }
                )
                events.append(
                    {
                        "type": "content_block_start",
                        "index": event["output_index"],
                        "content_block": block,
                    }
                )
            elif kind in (
                "response.output_text.delta",
                "response.function_call_arguments.delta",
            ):
                delta = (
                    {"type": "text_delta", "text": event["delta"]}
                    if kind == "response.output_text.delta"
                    else {"type": "input_json_delta", "partial_json": event["delta"]}
                )
                events.append(
                    {
                        "type": "content_block_delta",
                        "index": event["output_index"],
                        "delta": delta,
                    }
                )
            elif kind == "response.output_item.done":
                events.append(
                    {"type": "content_block_stop", "index": event["output_index"]}
                )
            elif kind in ("response.completed", "response.incomplete"):
                result = self.result()
                events += [
                    {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": result["stop_reason"],
                            "stop_sequence": None,
                        },
                        "usage": result["usage"],
                    },
                    {"type": "message_stop"},
                ]
        return events
