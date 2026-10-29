"""Turn-local schedule proposals; the model has no confirmation capability."""

from typing import Any

from services.orchestrator.loop import Call, Message
from services.retrieval.project_schedules import TaskInput


class ScheduleTools:
    def __init__(
        self, broker: Any, turn: dict[str, Any], timezone: str, locale: str
    ) -> None:
        self.broker, self.turn, self.timezone, self.locale = (
            broker,
            turn,
            timezone,
            locale,
        )

    def declaration(self) -> Message:
        parameters = TaskInput.model_json_schema()
        for key in ("enabled", "daily_cap_eur", "locale"):
            parameters["properties"].pop(key)
        schedule = parameters["$defs"]["TaskSchedule"]
        for key in ("timezone", "anchor"):
            schedule["properties"].pop(key)
        schedule["required"] = [
            key for key in schedule["required"] if key != "timezone"
        ]
        for schema in (
            parameters,
            schedule,
            *parameters["properties"].values(),
            *schedule["properties"].values(),
        ):
            schema.pop("title", None)
            schema.pop("default", None)
        return dict(
            type="function",
            function=dict(
                name="propose_scheduled_task",
                description=(
                    "For requested recurring work only: do the one-off part now, then propose its instruction, cadence and file. "
                    "Weekday Monday=0; default time 09:00. Creates a disabled confirmation card; only the user enables it. Time zone: "
                    + self.timezone
                ),
                parameters=parameters,
            ),
        )

    async def execute(self, call: Call) -> Message:
        from services.orchestrator.api_key_edge import key_call

        if key_call.get():
            return dict(error="permission_denied")
        import json

        payload = json.loads(call.arguments)
        if not isinstance(payload, dict) or not isinstance(
            payload.get("schedule"), dict
        ):
            raise ValueError("A task and schedule object are required")
        payload["schedule"]["timezone"] = self.timezone
        data = TaskInput.model_validate(payload)
        data.schedule.timezone = self.timezone
        payload = data.model_dump(mode="json")
        payload["locale"] = (
            self.locale if self.locale in {"en", "fr", "de", "es", "it"} else "en"
        )
        result = await self.broker.call(
            "schedule-propose",
            dict(
                conversation_id=self.turn["conversation_id"],
                request_id=self.turn["request_id"],
                task=payload,
            ),
        )
        return dict(proposal_id=result["id"], enabled=False, confirmation_required=True)


def publish_declaration() -> Message:
    from pydantic import BaseModel, ConfigDict, Field

    class Output(BaseModel):
        model_config = ConfigDict(extra="forbid")
        content: str = Field(min_length=1, max_length=200000)

    return dict(
        type="function",
        function=dict(
            name="publish_scheduled_file",
            description="Publish UTF-8 text to this scheduled task's exact target file, preserving the previous version. Use for Markdown, text, CSV, JSON or source code. For PDF and Office files use publish_document instead. Call once after completing the requested content.",
            parameters=Output.model_json_schema(),
        ),
    )
