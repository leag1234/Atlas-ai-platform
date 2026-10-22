"""Stateless Responses text/function compatibility for developer clients."""

import copy
from typing import Any
from services.orchestrator.dev_chat import Output


class Wire:
    def __init__(self, output: Output, request: dict[str, Any]) -> None:
        self.output, self.request = output, request
        self.items: list[dict[str, Any]] = []
        self.indices: dict[str, int] = {}
        self.sequence = 0

    def event(self, kind: str, **data: Any) -> dict[str, Any]:
        event = {"type": kind, "sequence_number": self.sequence, **copy.deepcopy(data)}
        self.sequence += 1
        return event

    def result(self, status: str | None = None) -> dict[str, Any]:
        status = status or (
            "incomplete" if self.output.reason == "length" else "completed"
        )
        usage = self.output.usage
        return {
            "id": str(self.output.base["id"]).replace("chatcmpl-", "resp_"),
            "object": "response",
            "created_at": self.output.base["created"],
            "model": "atlas-code",
            "status": status,
            "output": self.items,
            "error": None,
            "incomplete_details": {"reason": "max_output_tokens"}
            if status == "incomplete"
            else None,
            "instructions": self.request.get("instructions"),
            "metadata": self.request.get("metadata", {}),
            "tools": self.request.get("tools", []),
            "tool_choice": self.request.get("tool_choice", "auto"),
            "parallel_tool_calls": self.output.request.parallel_tool_calls,
            "store": False,
            "usage": None
            if usage is None
            else {
                "input_tokens": usage["prompt_tokens"],
                "output_tokens": usage["completion_tokens"],
                "total_tokens": usage["total_tokens"],
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0}
                if self.output.request.reasoning_effort == "none"
                else {},
            },
        }

    def start(self) -> list[dict[str, Any]]:
        return [
            self.event(kind, response=self.result("in_progress"))
            for kind in ("response.created", "response.in_progress")
        ]

    def feed(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        events = []
        item: dict[str, Any]
        if "error" in chunk:
            return [
                self.event(
                    "error", code="server_error", message="provider_error", param=None
                )
            ]
        for choice in chunk["choices"]:
            delta = choice["delta"]
            if delta.get("content"):
                if "text" not in self.indices:
                    index = self.indices["text"] = len(self.items)
                    item = {
                        "id": "msg_" + str(self.output.base["id"]),
                        "type": "message",
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    }
                    self.items.append(item)
                    events.append(
                        self.event(
                            "response.output_item.added", output_index=index, item=item
                        )
                    )
                    item["content"] = [
                        {
                            "type": "output_text",
                            "text": "",
                            "annotations": [],
                            "logprobs": [],
                        }
                    ]
                    events.append(
                        self.event(
                            "response.content_part.added",
                            output_index=index,
                            item_id=item["id"],
                            content_index=0,
                            part=item["content"][0],
                        )
                    )
                index = self.indices["text"]
                item = self.items[index]
                item["content"][0]["text"] += delta["content"]
                events.append(
                    self.event(
                        "response.output_text.delta",
                        output_index=index,
                        item_id=item["id"],
                        content_index=0,
                        delta=delta["content"],
                        logprobs=[],
                    )
                )
            for change in delta.get("tool_calls", []):
                key = str(change["index"])
                if key not in self.indices:
                    index = self.indices[key] = len(self.items)
                    call = self.output.calls[change["index"]]
                    item = {
                        "id": "fc_" + call["id"],
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": call["function"]["name"],
                        "arguments": "",
                        "status": "in_progress",
                    }
                    self.items.append(item)
                    events.append(
                        self.event(
                            "response.output_item.added", output_index=index, item=item
                        )
                    )
                index = self.indices[key]
                item = self.items[index]
                args = change["function"].get("arguments", "")
                item["arguments"] += args
                if args:
                    events.append(
                        self.event(
                            "response.function_call_arguments.delta",
                            output_index=index,
                            item_id=item["id"],
                            delta=args,
                        )
                    )
        if "usage" in chunk:
            for index, item in enumerate(self.items):
                item["status"] = (
                    "incomplete" if self.output.reason == "length" else "completed"
                )
                common = {"output_index": index, "item_id": item["id"]}
                if item["type"] == "message":
                    part = item["content"][0]
                    events.append(
                        self.event(
                            "response.output_text.done",
                            **common,
                            content_index=0,
                            text=part["text"],
                            logprobs=[],
                        )
                    )
                    events.append(
                        self.event(
                            "response.content_part.done",
                            **common,
                            content_index=0,
                            part=part,
                        )
                    )
                else:
                    events.append(
                        self.event(
                            "response.function_call_arguments.done",
                            **common,
                            name=item["name"],
                            arguments=item["arguments"],
                        )
                    )
                events.append(
                    self.event(
                        "response.output_item.done", output_index=index, item=item
                    )
                )
            result = self.result()
            events.append(self.event("response." + result["status"], response=result))
        return events
