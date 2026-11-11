"""Repair tool calls a model sends in a shape the client cannot run.

Measured on a coding client of the developer API (2026-11-10 and 11): the model called
tools that the request never declared (``bash``, ``exec``) and called the declared shell
tool with ``command`` instead of its required ``cmd``. The client refused each call; the
run then ended silently or looped until the loop guard. These are unambiguous slips with
one sensible reading, so the gateway rewrites them to the declared tool before the client
sees them. Anything ambiguous passes through unchanged.

Tool-call deltas are held until the call is complete (the client runs a call only once it
is complete anyway); text and reasoning stream as before.
"""

from __future__ import annotations

import json
import logging
from contextlib import aclosing
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from typing import Any

# Names a model uses for "run a shell command" when the declared tool has another name.
SHELL_ALIASES = {
    "bash",
    "sh",
    "zsh",
    "shell",
    "exec",
    "execute",
    "exec_command",
    "run",
    "run_command",
    "run_shell",
    "run_shell_command",
    "shell_command",
    "terminal",
    "command",
    "local_shell",
    "container_exec",
}
# Argument names that carry the command line, in the order they are tried.
COMMAND_KEYS = ("cmd", "command", "script", "code", "commands", "input")
DIRECTORY_KEYS = ("workdir", "cwd", "dir", "directory", "working_directory")


def _declared(tools: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """Declared function names and their JSON parameter schemas."""
    found: dict[str, dict[str, Any]] = {}
    for tool in tools:
        function = getattr(tool, "function", None)
        if function is None and isinstance(tool, dict):
            function = tool.get("function")
        name = getattr(function, "name", None) or (
            function.get("name") if isinstance(function, dict) else None
        )
        parameters = getattr(function, "parameters", None)
        if parameters is None and isinstance(function, dict):
            parameters = function.get("parameters")
        if isinstance(name, str):
            found[name] = parameters if isinstance(parameters, dict) else {}
    return found


def _command_field(schema: dict[str, Any]) -> tuple[str, str] | None:
    """The property that takes the command line, and its JSON type."""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    for key in COMMAND_KEYS[:3]:
        spec = properties.get(key)
        if isinstance(spec, dict) and spec.get("type") in ("string", "array"):
            return key, str(spec["type"])
    return None


def _shell_target(declared: dict[str, dict[str, Any]]) -> str | None:
    """The one declared tool that runs a shell command, if there is exactly one."""
    shells = [
        name
        for name, schema in declared.items()
        if name in SHELL_ALIASES and _command_field(schema) is not None
    ]
    return shells[0] if len(shells) == 1 else None


def _command_text(arguments: dict[str, Any]) -> str | None:
    for key in COMMAND_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            # ["bash", "-lc", "ls"] is one command line; a plain list is joined.
            if len(value) == 3 and value[1] in ("-c", "-lc"):
                return str(value[2])
            return " ".join(value)
    return None


def _shell_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> str | None:
    field = _command_field(schema)
    command = _command_text(arguments)
    if field is None or command is None:
        return None
    key, kind = field
    properties = schema.get("properties", {})
    repaired: dict[str, Any] = {
        key: command if kind == "string" else ["bash", "-lc", command]
    }
    for name in DIRECTORY_KEYS:
        if isinstance(arguments.get(name), str):
            target = next((d for d in DIRECTORY_KEYS if d in properties), None)
            if target is not None:
                repaired[target] = arguments[name]
            break
    # Other arguments the declared tool knows are kept as they are.
    for name, value in arguments.items():
        if name in properties and name not in repaired and name not in COMMAND_KEYS:
            repaired[name] = value
    return json.dumps(repaired, ensure_ascii=False)


def repair(
    name: str, arguments: str, declared: dict[str, dict[str, Any]]
) -> tuple[str, str, str] | None:
    """(name, arguments, reason) for a call to rewrite, or None to leave it as sent."""
    try:
        parsed = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        parsed = None
    if name in declared:
        schema = declared[name]
        field = _command_field(schema)
        if field is None or not isinstance(parsed, dict):
            return None
        required = schema.get("required") or []
        if field[0] in parsed or field[0] not in required:
            return None
        fixed = _shell_arguments(parsed, schema)
        return (name, fixed, "argument_name") if fixed is not None else None
    target = _shell_target(declared)
    if target is None or name not in SHELL_ALIASES:
        return None
    if parsed is None:
        # A bare command line instead of JSON arguments.
        parsed = {"command": arguments}
    if not isinstance(parsed, dict):
        return None
    fixed = _shell_arguments(parsed, declared[target])
    return (target, fixed, "tool_name") if fixed is not None else None


def _held(name: str, declared: dict[str, dict[str, Any]]) -> bool:
    """Whether a call may need a repair, so it is held until complete.

    An undeclared name, a missing name, or the declared shell tool (its arguments
    may use the wrong name). Every other call streams as it comes.
    """
    if not name or name not in declared:
        return True
    schema = declared[name]
    field = _command_field(schema)
    return field is not None and field[0] in (schema.get("required") or [])


async def repaired_frames(
    frames: AsyncIterator[dict[str, Any]], tools: Sequence[Any]
) -> AsyncGenerator[dict[str, Any], None]:
    """Provider chat frames, with tool calls that need it repaired before they leave."""
    async with aclosing(frames) as source:  # type: ignore[type-var]
        async for frame in _repair(source, _declared(tools)):
            yield frame


async def _repair(
    frames: AsyncIterator[dict[str, Any]], declared: dict[str, dict[str, Any]]
) -> AsyncGenerator[dict[str, Any], None]:
    if not declared:
        async for frame in frames:
            yield frame
        return
    # Per choice: held calls in provider order, and (index, id) of open calls.
    held: dict[int, list[dict[str, Any]]] = {}
    open_calls: dict[int, dict[int, dict[str, Any]]] = {}
    # Once a call of a choice is held, the later ones are held too: the client
    # must receive call indices in order.
    holding: set[int] = set()

    def flush(position: int) -> dict[str, Any] | None:
        calls = held.pop(position, [])
        holding.discard(position)
        open_calls.pop(position, None)
        if not calls:
            return None
        out = []
        for call in calls:
            fixed = repair(call["name"], call["arguments"], declared)
            if fixed is not None:
                logging.getLogger("atlas.api").warning(
                    json.dumps(
                        {
                            "event": "devapi_call_repaired",
                            "reason": fixed[2],
                            "sent": call["name"][:64],
                            "declared": fixed[0],
                        }
                    )
                )
                call = dict(call, name=fixed[0], arguments=fixed[1])
            out.append(
                {
                    "index": call["index"],
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call["arguments"]},
                }
            )
        return {
            "choices": [
                {"index": position, "delta": {"tool_calls": out}, "finish_reason": None}
            ]
        }

    async for frame in frames:
        choices = frame.get("choices") or []
        finished: list[int] = []
        passed = []
        for choice in choices:
            position = choice.get("index", 0)
            delta = dict(choice.get("delta") or {})
            streamed = []
            for call in delta.pop("tool_calls", None) or []:
                index = call.get("index", 0)
                new_id = call.get("id") or ""
                function = call.get("function") or {}
                known = open_calls.setdefault(position, {})
                current = known.get(index)
                if current is None or (
                    new_id and current["id"] and new_id != current["id"]
                ):
                    # A new call opens (some providers reuse index 0 with a new id).
                    current = {
                        "index": index,
                        "id": new_id,
                        "name": function.get("name") or "",
                        "arguments": "",
                        "held": position in holding
                        or _held(function.get("name") or "", declared),
                    }
                    known[index] = current
                    if current["held"]:
                        holding.add(position)
                        held.setdefault(position, []).append(current)
                if not current["held"]:
                    streamed.append(call)
                    continue
                if new_id and not current["id"]:
                    current["id"] = new_id
                if function.get("name") and not current["name"]:
                    current["name"] = function["name"]
                current["arguments"] += function.get("arguments") or ""
            if streamed:
                delta["tool_calls"] = streamed
            if choice.get("finish_reason") is not None:
                finished.append(position)
            if delta or choice.get("finish_reason") is not None:
                passed.append(dict(choice, delta=delta))
        # Held calls go out just before the frame that finishes their choice.
        for position in finished:
            complete = flush(position)
            if complete is not None:
                yield complete
        if passed or "usage" in frame:
            yield dict(frame, choices=passed)
        elif not choices:
            yield frame
    # A stream that ended without finish_reason still delivers what it held.
    for position in list(held):
        complete = flush(position)
        if complete is not None:
            yield complete
