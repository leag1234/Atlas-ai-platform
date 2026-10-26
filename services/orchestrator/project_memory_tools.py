"""Use the turn-bound private memory broker from both Chat and Work."""

import json
from typing import Any

from packages.project_memory_tool import MemoryTool
from services.orchestrator.loop import Call, Message
from services.retrieval.project_identity import ProjectError


class MemoryTools:
    def __init__(
        self, broker: Any, state: dict[str, Any], generation: int | None = None
    ) -> None:
        self.broker, self.state, self.generation = broker, state, generation

    async def execute(self, call: Call) -> Message:
        try:
            arguments = MemoryTool.model_validate_json(call.arguments).model_dump(
                mode="json"
            )
            result = await self.broker.call(
                "memory",
                dict(
                    conversation_id=self.state["turn"]["conversation_id"],
                    request_id=self.state["turn"]["request_id"],
                    generation=self.generation,
                    arguments=arguments,
                ),
            )
            self.state["context"]["memory_revision"] = result["memory_revision"]
            self.state["turn"]["context_revision"] = result["memory_revision"]
            return dict(trust="untrusted", data=result)
        except (ValueError, json.JSONDecodeError):
            return {"error": "invalid_memory_arguments"}
        except ProjectError as exc:
            return dict(exc.body)
