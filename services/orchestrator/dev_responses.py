"""Stateless Responses text/function compatibility for developer clients."""

import copy
import json
from typing import Any
from services.orchestrator.dev_chat import Output, url_citations


def reasoning_tokens(usage: Any) -> int:
    details = (
        usage.get("completion_tokens_details") if isinstance(usage, dict) else None
    )
    value = details.get("reasoning_tokens") if isinstance(details, dict) else None
    return value if isinstance(value, int) and value >= 0 else 0


ENVELOPE = (
    "*** Begin Patch",
    "*** End Patch",
    "*** Add File:",
    "*** Update File:",
    "*** Delete File:",
)


def v4a_body(diff: str) -> str:
    """The diff of one apply_patch operation, without the envelope of the freeform patch tool.

    The Responses apply_patch tool carries one file per operation, so its diff has no
    *** Begin Patch, *** End Patch or file header lines; models sometimes still write them.
    """
    lines = diff.split("\n")
    kept = [line for line in lines if not line.startswith(ENVELOPE)]
    return "\n".join(kept) if len(kept) != len(lines) else diff


class Wire:
    def __init__(self, output: Output, request: dict[str, Any]) -> None:
        self.output, self.request = output, request
        self.items: list[dict[str, Any]] = []
        self.indices: dict[str, int] = {}
        self.sequence = 0
        # True once response.completed, .incomplete or .failed has been emitted.
        self.terminal = False

    def event(self, kind: str, **data: Any) -> dict[str, Any]:
        event = {"type": kind, "sequence_number": self.sequence, **copy.deepcopy(data)}
        self.sequence += 1
        return event

    def result(self, status: str | None = None) -> dict[str, Any]:
        status = status or (
            "incomplete" if self.output.reason == "length" else "completed"
        )
        usage = self.output.usage
        result = {
            "id": str(self.output.base["id"]).replace("chatcmpl-", "resp_"),
            "object": "response",
            "created_at": self.output.base["created"],
            "model": self.output.base["model"],
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
            "text": self.request.get("text", {"format": {"type": "text"}}),
            "usage": None
            if usage is None
            else {
                "input_tokens": usage["prompt_tokens"],
                "output_tokens": usage["completion_tokens"],
                "total_tokens": usage["total_tokens"],
                "input_tokens_details": {
                    "cached_tokens": (usage.get("prompt_tokens_details") or {}).get(
                        "cached_tokens", 0
                    )
                },
                # Responses clients (Codex) require the field. Report the provider's
                # count when it gives one; providers that hide reasoning give 0.
                "output_tokens_details": {"reasoning_tokens": reasoning_tokens(usage)},
            },
        }
        if self.output.served_by:
            result["served_by"] = self.output.served_by
        if self.output.privacy:
            result["privacy"] = self.output.privacy
        return result

    def start(self) -> list[dict[str, Any]]:
        return [
            self.event(kind, response=self.result("in_progress"))
            for kind in ("response.created", "response.in_progress")
        ]

    def tool_kind(self, name: str) -> str:
        for tool in self.request.get("tools", []):
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "custom" and tool.get("name") == name:
                return "custom"
            if tool.get("type") == "local_shell" and name == "local_shell":
                return "local_shell"
            if tool.get("type") in ("shell", "apply_patch") and name == tool["type"]:
                return str(tool["type"])
        return "function"

    def new_item(self, kind: str, call: dict[str, Any]) -> dict[str, Any]:
        if kind in ("shell", "apply_patch"):
            # Client-executed tools of the Responses API: native items, filled as the
            # arguments stream.
            return {
                "id": ("sh_" if kind == "shell" else "apc_") + call["id"],
                "type": kind + "_call",
                "call_id": call["id"],
                **(
                    {"action": {}, "environment": {"type": "local"}}
                    if kind == "shell"
                    else {"operation": {}}
                ),
                "status": "in_progress",
            }
        return {
            "id": (
                "ctc_"
                if kind == "custom"
                else "lsh_"
                if kind == "local_shell"
                else "fc_"
            )
            + call["id"],
            "type": "custom_tool_call"
            if kind == "custom"
            else "local_shell_call"
            if kind == "local_shell"
            else "function_call",
            "call_id": call["id"],
            "name": call["function"]["name"],
            **(
                {"input": ""}
                if kind == "custom"
                else {"action": {}}
                if kind == "local_shell"
                else {"arguments": ""}
            ),
            "status": "in_progress",
        }

    def feed(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        events = []
        item: dict[str, Any]
        if "hosted" in chunk:
            hosted = chunk["hosted"]
            index = len(self.items)
            item = {
                "id": "ws_" + str(hosted["id"]),
                "type": "web_search_call",
                "status": "completed",
                "action": {
                    "type": "search",
                    "query": hosted["query"],
                    "sources": [
                        {"type": "url", "url": row["url"]} for row in hosted["results"]
                    ],
                },
            }
            self.items.append(item)
            return [
                self.event(
                    "response.output_item.added",
                    output_index=index,
                    item=dict(item, status="in_progress"),
                ),
                self.event(
                    "response.web_search_call.completed",
                    output_index=index,
                    item_id=item["id"],
                ),
                self.event("response.output_item.done", output_index=index, item=item),
            ]
        if "error" in chunk:
            code = (
                chunk["error"].get("type", "server_error")
                if chunk["error"].get("type") == "invalid_response_format"
                else "server_error"
            )
            message = (
                chunk["error"].get("message", "invalid_response_format")
                if chunk["error"].get("type") == "invalid_response_format"
                else "provider_error"
            )
            # Clients such as Codex wait for a terminal response event; without one an
            # error mid-stream looks like a dropped connection (reported 2026-10-31).
            failed = self.result("failed") | {
                "error": {"code": code, "message": message}
            }
            self.terminal = True
            return [
                self.event("error", code=code, message=message, param=None),
                self.event("response.failed", response=failed),
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
                    kind = self.tool_kind(call["function"]["name"])
                    item = self.new_item(kind, call)
                    self.items.append(item)
                    events.append(
                        self.event(
                            "response.output_item.added", output_index=index, item=item
                        )
                    )
                index = self.indices[key]
                item = self.items[index]
                args = change["function"].get("arguments", "")
                if item["type"] in ("shell_call", "apply_patch_call"):
                    try:
                        parsed = json.loads(
                            self.output.calls[change["index"]]["function"]["arguments"]
                        )
                    except (TypeError, ValueError):
                        parsed = {}
                    if not isinstance(parsed, dict):
                        parsed = {}
                    if item["type"] == "shell_call":
                        item["action"] = {
                            "commands": parsed.get("commands", []),
                            "timeout_ms": parsed.get("timeout_ms"),
                            "max_output_length": parsed.get("max_output_length"),
                        }
                    else:
                        operation = parsed.get("operation", {})
                        if isinstance(operation, dict) and isinstance(
                            operation.get("diff"), str
                        ):
                            operation = dict(
                                operation, diff=v4a_body(operation["diff"])
                            )
                        item["operation"] = operation
                    continue
                if item["type"] == "custom_tool_call":
                    try:
                        input_value = json.loads(
                            self.output.calls[change["index"]]["function"]["arguments"]
                        )["input"]
                    except (KeyError, TypeError, ValueError):
                        input_value = ""
                    previous = item["input"]
                    item["input"] = input_value
                elif item["type"] == "local_shell_call":
                    try:
                        item["action"] = json.loads(
                            self.output.calls[change["index"]]["function"]["arguments"]
                        )
                    except (TypeError, ValueError):
                        item["action"] = {}
                else:
                    item["arguments"] += args
                if args:
                    if item["type"] == "custom_tool_call":
                        delta = item["input"][len(previous) :]
                        if delta:
                            events.append(
                                self.event(
                                    "response.custom_tool_call_input.delta",
                                    output_index=index,
                                    item_id=item["id"],
                                    delta=delta,
                                )
                            )
                    elif item["type"] == "function_call":
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
                    # Retrieved urls that the answer quotes, as Responses url_citation.
                    part["annotations"] = [
                        {"type": "url_citation", **annotation["url_citation"]}
                        for annotation in url_citations(
                            part["text"], self.output.citations
                        )
                    ]
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
                elif item["type"] == "custom_tool_call":
                    events.append(
                        self.event(
                            "response.custom_tool_call_input.done",
                            **common,
                            name=item["name"],
                            input=item["input"],
                        )
                    )
                elif item["type"] == "function_call":
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
            self.terminal = True
            events.append(
                self.event(
                    "response." + result["status"],
                    response=result,
                    **(
                        {"served_by": self.output.served_by}
                        if self.output.served_by
                        else {}
                    ),
                )
            )
        return events
