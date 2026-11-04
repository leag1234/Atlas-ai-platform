"""Messages events over the shared validated text/function stream."""

import hashlib
from typing import Any
from pydantic_core import from_json
from services.orchestrator.dev_chat import Output
from services.orchestrator.dev_responses import Wire as ResponsesWire


class Wire:
    def __init__(self, output: Output, request: dict[str, Any]) -> None:
        self.output = output
        # Thinking blocks only when the client asked for thinking, as Messages does.
        thinking = request.get("thinking")
        self.thinking = isinstance(thinking, dict) and thinking.get("type") in (
            "enabled",
            "adaptive",
        )
        self.core = ResponsesWire(output, request, reasoning=self.thinking)
        # Messages blocks never overlap: the thinking block is signed and closed when
        # the next block opens, and its end-of-answer events are then skipped.
        self.thinking_open: int | None = None
        self.closed: set[int] = set()
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

    def citation(self, url: str, title: str) -> dict[str, Any]:
        """A Messages web search citation for a retrieved url the answer quotes."""
        snippet = next(
            (
                str(source.get("snippet", ""))
                for source in self.output.citations
                if source.get("url") == url
            ),
            "",
        )
        return {
            "type": "web_search_result_location",
            "url": url,
            "title": title,
            "encrypted_index": "",
            "cited_text": snippet[:150],
        }

    @staticmethod
    def signature(text: str) -> str:
        """Opaque signature of a thinking block; clients send it back unchanged."""
        return "atlas-" + hashlib.sha256(text.encode()).hexdigest()

    @staticmethod
    def fetch_blocks(item: dict[str, Any]) -> list[dict[str, Any]]:
        identifier = "srvtoolu_" + str(item["id"]).removeprefix("wf_")
        result: dict[str, Any] = (
            {"type": "web_fetch_tool_error", "error_code": item["error"]}
            if "error" in item
            else {
                "type": "web_fetch_result",
                "url": item["url"],
                "content": {
                    "type": "document",
                    "source": {
                        "type": "text",
                        "media_type": "text/plain",
                        "data": item.get("text", ""),
                    },
                },
                "retrieved_at": item.get("retrieved_at", ""),
            }
        )
        return [
            {
                "type": "server_tool_use",
                "id": identifier,
                "name": "web_fetch",
                "input": {"url": item["url"]},
            },
            {
                "type": "web_fetch_tool_result",
                "tool_use_id": identifier,
                "content": result,
            },
        ]

    def hosted_blocks(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        if item["type"] == "web_fetch_call":
            return self.fetch_blocks(item)
        return self.search_blocks(item)

    def result(self) -> dict[str, Any]:
        content = []
        searches = fetches = 0
        for item in self.core.items:
            if item["type"] == "web_fetch_call":
                fetches += 1
                content += self.fetch_blocks(item)
            elif item["type"] == "web_search_call":
                searches += 1
                content += self.search_blocks(item)
            elif item["type"] == "message":
                part = item["content"][0]
                block: dict[str, Any] = {"type": "text", "text": part["text"]}
                cited = [
                    self.citation(note["url"], note.get("title", ""))
                    for note in part.get("annotations", [])
                    if note.get("type") == "url_citation"
                ]
                if cited:
                    block["citations"] = cited
                content.append(block)
            elif item["type"] == "reasoning":
                text = item["content"][0]["text"]
                content.append(
                    {
                        "type": "thinking",
                        "thinking": text,
                        "signature": self.signature(text),
                    }
                )
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
                    {
                        "server_tool_use": {
                            "web_search_requests": searches,
                            **({"web_fetch_requests": fetches} if fetches else {}),
                        }
                    }
                    if searches or fetches
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
            if (
                kind in ("response.reasoning_text.done", "response.output_item.done")
                and event["output_index"] in self.closed
            ):
                continue
            if (
                kind == "response.output_item.added"
                and event["item"]["type"] != "reasoning"
                and self.thinking_open is not None
            ):
                index, self.thinking_open = self.thinking_open, None
                self.closed.add(index)
                text = self.core.items[index]["content"][0]["text"]
                events += [
                    {
                        "type": "content_block_delta",
                        "index": self.block(index),
                        "delta": {
                            "type": "signature_delta",
                            "signature": self.signature(text),
                        },
                    },
                    {"type": "content_block_stop", "index": self.block(index)},
                ]
            if (
                kind == "response.output_item.added"
                and event["item"]["type"] == "reasoning"
            ):
                self.thinking_open = event["output_index"]
            if kind == "error":
                events.append(
                    {
                        "type": "error",
                        "error": {"type": "api_error", "message": "provider_error"},
                    }
                )
            elif kind == "response.output_item.added" and event["item"]["type"] in (
                "web_search_call",
                "web_fetch_call",
            ):
                continue
            elif kind == "response.output_item.done" and event["item"]["type"] in (
                "web_search_call",
                "web_fetch_call",
            ):
                index = event["output_index"]
                if index in self.extra:
                    # The final pass repeats done for every item: the blocks are out.
                    continue
                self.extra[index] = 1
                first = self.block(index)
                for offset, search_block in enumerate(
                    self.hosted_blocks(event["item"])
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
                    {"type": "thinking", "thinking": "", "signature": ""}
                    if item["type"] == "reasoning"
                    else {"type": "text", "text": ""}
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
            elif kind == "response.output_text.annotation.added":
                note = event["annotation"]
                events.append(
                    {
                        "type": "content_block_delta",
                        "index": self.block(event["output_index"]),
                        "delta": {
                            "type": "citations_delta",
                            "citation": self.citation(
                                note["url"], note.get("title", "")
                            ),
                        },
                    }
                )
            elif kind == "response.reasoning_text.delta":
                events.append(
                    {
                        "type": "content_block_delta",
                        "index": self.block(event["output_index"]),
                        "delta": {"type": "thinking_delta", "thinking": event["delta"]},
                    }
                )
            elif kind == "response.reasoning_text.done":
                events.append(
                    {
                        "type": "content_block_delta",
                        "index": self.block(event["output_index"]),
                        "delta": {
                            "type": "signature_delta",
                            "signature": self.signature(event["text"]),
                        },
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
