"""Best-effort state updates; tasks never join the interactive answer path."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

import yaml

from packages import context_usage
from packages.ports import atlas_ports
from pydantic import ValidationError

from packages.model_json import aliases, object_content
from packages.session_state import SessionState, StateUpdate
from services.orchestrator.model import GatewayModel

log = logging.getLogger(__name__)
tasks: set[asyncio.Task[None]] = set()
locks: WeakValueDictionary[tuple[str, str, str], asyncio.Lock] = WeakValueDictionary()


def parse_update(result: Any) -> StateUpdate:
    if not isinstance(result, dict):
        raise ValueError("session_update_invalid_response")
    text = result.get("text")
    calls = result.get("calls", [])
    if isinstance(text, str) and text.strip() and not calls:
        value = object_content(text)
    elif (
        isinstance(calls, list)
        and len(calls) == 1
        and isinstance(calls[0], dict)
        and calls[0].get("name") == "update_session_state"
        and isinstance(calls[0].get("arguments"), str)
    ):
        value = object_content(calls[0]["arguments"])
    else:
        raise ValueError("session_update_ambiguous_or_missing_payload")
    for field in SessionState.model_fields:
        if isinstance(value.get(field), list):
            value[field] = [aliases(entry, "state_entry") for entry in value[field]]
    return StateUpdate.model_validate(value)


def failure_reason(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "invalid_schema"
    if isinstance(exc, json.JSONDecodeError):
        return "invalid_json"
    safe = {
        "ambiguous_field_alias",
        "duplicate_json_field",
        "json_object_required",
        "session_update_invalid_response",
        "session_update_ambiguous_or_missing_payload",
        "session_update_invalid_usage",
    }
    return (
        str(exc)
        if isinstance(exc, ValueError) and str(exc) in safe
        else "update_failed"
    )


def schedule(broker: Any, completed: dict[str, Any]) -> None:
    task = asyncio.create_task(update(broker, completed), name="project-session-state")
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def update(broker: Any, completed: dict[str, Any]) -> None:
    turn = completed["turn"]
    key = broker.org, broker.project, turn["conversation_id"]
    lock = locks.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            binding = dict(
                conversation_id=turn["conversation_id"], request_id=turn["request_id"]
            )
            state = await broker.call("state-read", binding)
            if not state["turns"]:
                return
            routing = yaml.safe_load(
                Path("services/model-gateway/routing.yaml").read_text()
            )
            profile = next(
                k for k, v in routing["public_profiles"].items() if v == "fast"
            )
            schema = StateUpdate.model_json_schema()
            # Inline definitions to keep the requested JSON shape self-contained.
            entry = schema.pop("$defs")["Entry"]
            for field in schema["properties"].values():
                if field.get("items", {}).get("$ref"):
                    field["items"] = entry
            payload = dict(
                messages=[
                    dict(
                        role="system",
                        content="Update the typed session state from the completed turns, retaining exact facts and provenance. Entries with the same ID replace prior entries; omitted entries are retained. Resolve questions explicitly and replace the plan only when it changes. Project constraints remain separate authoritative data. Return only a JSON object in message content matching this schema, without tool calls: "
                        + json.dumps(schema),
                    ),
                    dict(
                        role="user",
                        content=json.dumps(
                            dict(
                                previous_session_state=state["session_state"],
                                completed_turns=state["turns"],
                                turn_evidence=state.get("evidence", []),
                                work_plan=state.get("work_plan", []),
                            )
                        ),
                    ),
                ],
                tools=[],
                response_format={"type": "json_object"},
                profile=profile,
                max_tokens=3000,
                timeout=30.0,
                budget_eur=str(state["budget_eur"]),
                local_enabled=False,
            )
            from services.orchestrator.project_transcript import DispatchEvents

            events = DispatchEvents(broker, completed)
            events.state_update = True
            events.session_id = state["session_id"]
            await events.publish("intent", payload)
            result = await GatewayModel.post(
                os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway"))
                + "/agent/complete",
                payload,
                30,
            )
            await events.publish("acknowledged", result)
            parsed = parse_update(result)
            from services.retrieval.project_work import empty_usage

            usage = empty_usage()
            incoming = result.get("usage", {})
            if not isinstance(incoming, dict):
                raise ValueError("session_update_invalid_usage")
            usage.update(
                model_calls=1,
                input_tokens=incoming.get("prompt_tokens", 0),
                output_tokens=incoming.get("completion_tokens", 0),
                cost_eur=float(str(result.get("cost_eur", 0))),
            )
            await broker.call(
                "state-write",
                dict(
                    binding,
                    previous_turn_id=state["previous_turn_id"],
                    represented_turn_ids=state["represented_turn_ids"]
                    + [t["id"] for t in state["turns"]],
                    update=parsed.model_dump(),
                    usage=usage,
                ),
                turn["id"] + ":state",
            )
    except asyncio.CancelledError:
        log.warning("session_state_update_cancelled turn=%s", turn["id"])
        raise
    except Exception as exc:
        # Upstream exception strings can contain request data. Log only the type.
        log.warning(
            "session_state_update_failed turn=%s error_type=%s reason=%s retry=next_turn",
            turn["id"],
            type(exc).__name__,
            failure_reason(exc),
        )
    finally:
        context_usage.samples.set(None)
