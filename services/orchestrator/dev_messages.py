"""Messages events over the shared validated text/function stream."""

from typing import Any
from pydantic_core import from_json
from services.orchestrator.dev_chat import Output
from services.orchestrator.dev_responses import Wire as ResponsesWire


class Wire:
    def __init__(self, output: Output, request: dict[str, Any]) -> None:
        self.output = output
        self.core = ResponsesWire(output, request)
        # A web search is two Messages blocks (server_tool_use, web_search_tool_result)
        # for one Responses item: core output indexes are shifted accordingly.
        self.extra: dict[int, int] = {}

    def block(self, output_index: int) -> int:
        return output_index + sum(1 for index in self.extra if index < output_index)

    @staticmethod
    def search_blocks(item: dict[str, Any]) -> list[dict[str, Any]]:
        identifier = "srvtoolu_" + str(item["id"]).removeprefix("ws_")
        return [
            {
                "type": "server_tool_use",
                "id": identifier,
                "name": "web_search",
                "input": {"query": item["action"]["query"]},
            },
            {
                "type": "web_search_tool_result",
                "tool_use_id": identifier,
                "content": [
                    {
                        "type": "web_search_result",
                        "url": source["url"],
                        "title": source.get("title", ""),
                        "encrypted_content": "",
                        "page_age": None,
                    }
                    for source in item["action"]["sources"]
                ],
            },
        ]

    def result(self) -> dict[str, Any]:
        content = []
        searches = 0
        for item in self.core.items:
            if item["type"] == "web_search_call":
                searches += 1
                content += self.search_blocks(item)
            elif item["type"] == "message":
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
        result = {
            "id": "msg_" + str(self.output.base["id"]),
            "type": "message",
            "role": "assistant",
            "model": self.output.base["model"],
            "content": content,
            "stop_reason": "stop_sequence"
            if self.output.stop_sequence is not None
            else {
                "stop": "end_turn",
                "tool_calls": "tool_use",
                "length": "max_tokens",
            }.get(self.output.reason or ""),
            "stop_sequence": self.output.stop_sequence,
            "usage": {
                "input_tokens": usage["prompt_tokens"],
                "output_tokens": usage["completion_tokens"],
                **(
                    {"server_tool_use": {"web_search_requests": searches}}
                    if searches
                    else {}
                ),
            },
        }
        if self.output.served_by:
            result["served_by"] = self.output.served_by
        if self.output.privacy:
            result["privacy"] = self.output.privacy
        return result

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
            elif (
                kind == "response.output_item.added"
                and event["item"]["type"] == "web_search_call"
            ):
                continue
            elif (
                kind == "response.output_item.done"
                and event["item"]["type"] == "web_search_call"
            ):
                index = event["output_index"]
                if index in self.extra:
                    # The final pass repeats done for every item: the blocks are out.
                    continue
                self.extra[index] = 1
                first = self.block(index)
                for offset, search_block in enumerate(
                    self.search_blocks(event["item"])
                ):
                    events += [
                        {
                            "type": "content_block_start",
                            "index": first + offset,
                            "content_block": search_block,
                        },
                        {"type": "content_block_stop", "index": first + offset},
                    ]
            elif kind == "response.web_search_call.completed":
                continue
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
                        "index": self.block(event["output_index"]),
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
                        "index": self.block(event["output_index"]),
                        "delta": delta,
                    }
                )
            elif kind == "response.output_item.done":
                events.append(
                    {
                        "type": "content_block_stop",
                        "index": self.block(event["output_index"]),
                    }
                )
            elif kind in ("response.completed", "response.incomplete"):
                result = self.result()
                events += [
                    {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": result["stop_reason"],
                            "stop_sequence": result["stop_sequence"],
                        },
                        "usage": result["usage"],
                    },
                    {
                        "type": "message_stop",
                        **(
                            {"served_by": self.output.served_by}
                            if self.output.served_by
                            else {}
                        ),
                    },
                ]
        return events
