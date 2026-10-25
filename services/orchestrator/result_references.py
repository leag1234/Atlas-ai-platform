"""Per-request context projection; originals live behind the scoped transcript broker."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from packages import tool_results
from packages.context_usage import register
from services.orchestrator.loop import Call, Message
from services.orchestrator.stream_client import sink_context
from services.retrieval.project_identity import ProjectError


class ResultReferences:
    def __init__(
        self,
        broker: Any,
        conversation: str,
        request: str,
        generation: int | None = None,
    ) -> None:
        self.broker = broker
        self.request_id = request
        self.binding = dict(
            conversation_id=conversation, request_id=request, generation=generation
        )
        self.messages: dict[str, Message] = {}

    async def retain(
        self, call: Call, output: Message, messages: list[Message]
    ) -> None:
        identifier = self.request_id + ":" + str(uuid4())
        content = json.dumps(output, ensure_ascii=False)
        result = await self.broker.call(
            "result-put",
            dict(self.binding, id=identifier, tool=call.name, content=content),
            identifier,
        )
        message: Message = dict(
            role="tool",
            tool_call_id=call.id,
            content=tool_results.full(identifier, content),
        )
        self.messages[identifier] = message
        messages.append(message)
        if call.name in tool_results.PROTECTED:
            register(str(message["content"]), {"session_state": output})
        for cleared in result["cleared"]:
            prior = self.messages.get(cleared["id"])
            if prior is not None:
                prior["content"] = cleared["summary"]
        if sink := sink_context.get():
            for event in result["events"]:
                await sink({"project": event})

    async def reopen(self, call: Call) -> Message:
        try:
            identifier, selected = tool_results.arguments(call.arguments)
            return dict(
                await self.broker.call(
                    "result-open", dict(self.binding, id=identifier, range=selected)
                )
            )
        except (ValueError, TypeError):
            return {"error": "invalid_reopen_arguments"}
        except ProjectError as exc:
            if exc.status not in {404, 422}:
                raise
            return dict(exc.body)
