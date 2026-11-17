"""Repair tool calls a model sends in a shape the client cannot run.

Measured on a coding client of the developer API (2026-11-10 and 11): the model called
tools that the request never declared (``bash``, ``exec``) and called the declared shell
tool with ``command`` instead of its required ``cmd``. The client refused each call; the
run then ended silently or looped until the loop guard. These are unambiguous slips with
one sensible reading, so the gateway rewrites them to the declared tool before the client
sees them. Anything ambiguous passes through unchanged.

Tool-call deltas are held until the call is complete (the client runs a call only once it
is complete anyway); text and reasoning stream as before. Every complete call is then
checked: its arguments must be one JSON object (safe slips such as a trailing comma or a
code fence are rewritten) and carry the required parameters of a declared tool. A call
that stays unusable is asked again once, with a short correction; invalid arguments never
reach the client.
"""

from __future__ import annotations

import ast
import json
import logging
import math
from contextlib import aclosing
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from time import monotonic
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


# Arguments that are not one JSON object. Reported 2026-11-16 by an office-suite client:
# the first tool call of a turn had arguments that were not valid JSON, the client
# refused it and sent the whole request again (about two minutes lost). Every call is
# now complete and checked before it leaves: safe slips with one reading are rewritten,
# anything else is asked again once, and invalid arguments never reach the client.
# Seconds without a frame to the client while a call is held: an empty delta keeps
# idle-timeout clients connected during a long call (a whole file in one argument).
KEEPALIVE_S = 15.0


class InvalidToolArguments(ValueError):
    """The model sent unusable tool-call arguments, also after the corrected attempt."""

    def __init__(self, frame: dict[str, Any] | None = None) -> None:
        super().__init__("invalid_tool_arguments")
        # The provider frame that finished the attempt (it may carry the usage).
        self.frame = frame


def _no_constant(value: str) -> Any:
    raise ValueError("non_finite_number")


def _json_only(value: Any) -> bool:
    """Plain JSON values only: no tuple, set, bytes or non-string key."""
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_only(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _json_only(v) for k, v in value.items())
    return False


def _parse(text: str, strict: bool = True) -> Any:
    try:
        return json.loads(text, parse_constant=_no_constant, strict=strict)
    except (ValueError, RecursionError):
        return None


def _without_trailing_commas(text: str) -> str:
    """The text without commas placed just before a closing bracket (outside strings)."""
    out: list[str] = []
    in_string = escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "}]":
            position = len(out) - 1
            while position >= 0 and out[position] in " \t\r\n":
                position -= 1
            if position >= 0 and out[position] == ",":
                del out[position]
        out.append(character)
    return "".join(out)


def _unfenced(text: str) -> str | None:
    """The text inside a markdown code fence (```json ... ```), None without one."""
    if len(text) < 6 or not (text.startswith("```") and text.endswith("```")):
        return None
    inner = text[3:-3]
    language, newline, rest = inner.partition("\n")
    if newline and language.strip().replace("_", "").replace("-", "").isalnum():
        inner = rest
    return inner.strip()


def _python_literal(text: str) -> dict[str, Any] | None:
    """A Python dict literal ({'a': 'b'}, True, None) read without evaluating code."""
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    return value if isinstance(value, dict) and _json_only(value) else None


def normalized_arguments(arguments: str) -> tuple[str, str | None] | None:
    """(arguments, repair) when the text reads as one JSON object, else None.

    ``repair`` names the safe rewrite applied, None when the arguments are already one
    JSON object (they are then returned exactly as sent). A rewrite is accepted only when
    a strict JSON parser reads its result as one object.
    """
    value = _parse(arguments)
    if isinstance(value, dict):
        return arguments, None
    if isinstance(value, str) and isinstance(_parse(value), dict):
        return json.dumps(_parse(value), ensure_ascii=False), "double_encoded"
    text = arguments.strip()
    if not text:
        # Some models send nothing for a call without parameters.
        return "{}", "empty"
    repairs: list[str] = []
    fenced = _unfenced(text)
    if fenced is not None:
        text = fenced
        repairs.append("code_fence")
        value = _parse(text)
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False), "+".join(repairs)
    for candidate, repair_name in (
        (_without_trailing_commas(text), "trailing_comma"),
        (text, "control_character"),
        (_without_trailing_commas(text), "trailing_comma+control_character"),
    ):
        value = _parse(candidate, strict=repair_name == "trailing_comma")
        if isinstance(value, dict) and _json_only(value):
            return json.dumps(value, ensure_ascii=False), "+".join(
                [*repairs, repair_name]
            )
    value = _python_literal(text)
    if value is not None:
        return json.dumps(value, ensure_ascii=False), "+".join(
            [*repairs, "python_literal"]
        )
    return None


def _missing_required(
    name: str, arguments: str, declared: dict[str, dict[str, Any]]
) -> list[str]:
    schema = declared.get(name)
    required = schema.get("required") if schema else None
    if not isinstance(required, list):
        return []
    parsed = json.loads(arguments)
    return [key for key in required if isinstance(key, str) and key not in parsed]


def _log(event: str, **values: Any) -> None:
    # Metadata only: never arguments, tool output or text.
    logging.getLogger("atlas.api").warning(json.dumps({"event": event, **values}))


def checked_call(
    call: dict[str, Any], declared: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], str | None]:
    """The call ready to leave, and the problem that remains (None when it is usable).

    Problems: ``invalid_json`` (the arguments are not one JSON object) and
    ``missing_required`` (a declared tool called without a required argument).
    """
    normal = normalized_arguments(call["arguments"])
    if normal is None:
        # A bare command line sent to a shell alias is still one sensible call.
        fixed = repair(call["name"], call["arguments"], declared)
        if fixed is None or normalized_arguments(fixed[1]) is None:
            return call, "invalid_json"
    else:
        arguments, rewrite = normal
        if rewrite is not None:
            _log("devapi_call_arguments", outcome="repaired", repair=rewrite)
            call = dict(call, arguments=arguments)
        fixed = repair(call["name"], call["arguments"], declared)
    if fixed is not None:
        _log(
            "devapi_call_repaired",
            reason=fixed[2],
            sent=call["name"][:64],
            declared=fixed[0],
        )
        call = dict(call, name=fixed[0], arguments=fixed[1])
    if _missing_required(call["name"], call["arguments"], declared):
        return call, "missing_required"
    return call, None


def correction(problems: list[tuple[str, str, str]]) -> str:
    """The note sent with the corrected attempt (tool names only, never values)."""
    lines = []
    for name, problem, detail in problems:
        if problem == "invalid_json":
            lines.append(
                f"Your previous reply called the tool {name[:64]} with arguments that "
                f"are not valid JSON ({detail[:120]})."
            )
        else:
            lines.append(
                f"Your previous reply called the tool {name[:64]} without its required "
                f"arguments: {detail[:200]}."
            )
    return (
        " ".join(lines)
        + " Make the same tool calls again. The arguments of each call must be one valid"
        " JSON object with every required parameter of that tool."
    )


class _Retry(Exception):
    """Raised inside the first attempt: its calls are asked again."""

    def __init__(self, note: str, frame: dict[str, Any] | None) -> None:
        super().__init__("retry")
        self.note, self.frame = note, frame


async def repaired_frames(
    frames: AsyncIterator[dict[str, Any]],
    tools: Sequence[Any],
    restart: Callable[[str], AsyncIterator[dict[str, Any]]] | None = None,
    limit: int | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Provider chat frames, with every tool call complete, checked and usable.

    ``restart(note)`` opens one corrected attempt when a call stays unusable after the
    safe rewrites; its usage is added to the first attempt's. ``limit`` (micro-euros)
    is the reservation: no second attempt that could exceed it. Without a usable call
    after that, InvalidToolArguments is raised: invalid arguments never leave.
    """
    declared = _declared(tools)
    async with aclosing(frames) as source:  # type: ignore[type-var]
        if not declared:
            async for frame in source:
                yield frame
            return
        shown = _Shown()
        try:
            async for frame in _repair(source, declared, restart is not None):
                shown.see(frame)
                yield frame
            return
        except _Retry as signal:
            # The rest of the first attempt is only its usage.
            rest = [signal.frame] if signal.frame is not None else []
            rest += [frame async for frame in source]
            note = signal.note
        except InvalidToolArguments as failure:
            # No corrected attempt: the call is refused, its usage is still settled.
            rest = [failure.frame] if failure.frame is not None else []
            rest += [frame async for frame in source]
            for frame in rest:
                if "usage" in frame:
                    yield dict(frame, choices=[])
            raise
        prior = [frame for frame in rest if "usage" in frame]
        spent = sum(int(frame.get("cost_micro_eur", 0)) for frame in prior)
        assert restart is not None
        if limit is not None and spent * 2 > limit:
            _log("devapi_call_arguments", outcome="failed", reason="reservation")
            for frame in prior:
                yield dict(frame, choices=[])
            raise InvalidToolArguments()
        _log("devapi_call_arguments", outcome="retried")
    async with aclosing(restart(note)) as again:  # type: ignore[type-var]
        try:
            async for frame in shown.second(_repair(again, declared, False)):
                if "usage" in frame:
                    frame = _with_usage(frame, prior)
                yield frame
        except InvalidToolArguments as failure:
            # Both attempts are paid for: their usage still reaches the ledger.
            rest = [failure.frame] if failure.frame is not None else []
            rest += [frame async for frame in again]
            for frame in rest:
                if "usage" in frame:
                    yield dict(_with_usage(frame, prior), choices=[])
            raise
    _log("devapi_call_arguments", outcome="retry_succeeded")


def _with_usage(frame: dict[str, Any], prior: list[dict[str, Any]]) -> dict[str, Any]:
    """The final usage frame with the first attempt's tokens and cost added."""
    frame = dict(frame)
    usage = dict(frame["usage"])
    for old in prior:
        before = old["usage"]
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if isinstance(usage.get(key), int) and isinstance(before.get(key), int):
                usage[key] += before[key]
        for key in ("prompt_tokens_details", "completion_tokens_details"):
            if isinstance(usage.get(key), dict) and isinstance(before.get(key), dict):
                usage[key] = {
                    name: (
                        value + before[key][name]
                        if isinstance(value, int)
                        and isinstance(before[key].get(name), int)
                        else value
                    )
                    for name, value in usage[key].items()
                }
        if isinstance(frame.get("cost_micro_eur"), int) and isinstance(
            old.get("cost_micro_eur"), int
        ):
            frame["cost_micro_eur"] += old["cost_micro_eur"]
        if old.get("estimated_micro_eur"):
            frame["estimated_micro_eur"] = int(
                frame.get("estimated_micro_eur", 0)
            ) + int(old["estimated_micro_eur"])
    frame["usage"] = usage
    return frame


class _Shown:
    """What the first attempt already showed, so the second one never repeats it."""

    def __init__(self) -> None:
        self.started = False
        self.text = False

    def see(self, frame: dict[str, Any]) -> None:
        for choice in frame.get("choices") or []:
            delta = choice.get("delta") or {}
            self.started |= bool(delta)
            self.text |= bool(delta.get("content") or delta.get("reasoning_content"))

    async def second(
        self, frames: AsyncIterator[dict[str, Any]]
    ) -> AsyncGenerator[dict[str, Any], None]:
        # Text already shown stays: the corrected attempt adds only its calls. When it
        # answers without a call, its text follows what was shown.
        held: dict[int, str] = {}
        calls: set[int] = set()
        async for frame in frames:
            choices = []
            for choice in frame.get("choices") or []:
                position = choice.get("index", 0)
                delta = dict(choice.get("delta") or {})
                if self.started:
                    delta.pop("role", None)
                if self.text:
                    held[position] = held.get(position, "") + (
                        delta.pop("content", None) or ""
                    )
                    delta.pop("reasoning_content", None)
                if delta.get("tool_calls"):
                    calls.add(position)
                finish = choice.get("finish_reason")
                if finish is not None and position not in calls and held.get(position):
                    delta["content"] = "\n\n" + held.pop(position)
                if delta or finish is not None or not choice.get("delta"):
                    choices.append(dict(choice, delta=delta))
            if choices or "usage" in frame or not frame.get("choices"):
                yield dict(frame, choices=choices)


def _held_frame(position: int) -> dict[str, Any]:
    return {"choices": [{"index": position, "delta": {}, "finish_reason": None}]}


async def _repair(
    frames: AsyncIterator[dict[str, Any]],
    declared: dict[str, dict[str, Any]],
    retry: bool,
) -> AsyncGenerator[dict[str, Any], None]:
    # Per choice: held calls in provider order, and (index, id) of open calls.
    held: dict[int, list[dict[str, Any]]] = {}
    open_calls: dict[int, dict[int, dict[str, Any]]] = {}
    positions: set[int] = set()
    last_out = monotonic()

    def flush(position: int, frame: dict[str, Any] | None) -> dict[str, Any] | None:
        calls = held.pop(position, [])
        open_calls.pop(position, None)
        if not calls:
            return None
        # An answer cut by max_tokens says so in its finish_reason ("length"): its
        # last call is incomplete by nature and leaves as sent, without a retry.
        cut = any(
            choice.get("index", 0) == position
            and choice.get("finish_reason") == "length"
            for choice in (frame or {}).get("choices") or []
        )
        out, problems = [], []
        for call in calls:
            checked, problem = (call, None) if cut else checked_call(call, declared)
            if problem is not None:
                detail = (
                    _decode_error(checked["arguments"])
                    if problem == "invalid_json"
                    else ", ".join(
                        _missing_required(
                            checked["name"], checked["arguments"], declared
                        )
                    )
                )
                problems.append((checked["name"] or "(unnamed)", problem, detail))
            out.append(
                {
                    "index": checked["index"],
                    "id": checked["id"],
                    "type": "function",
                    "function": {
                        "name": checked["name"],
                        "arguments": checked["arguments"],
                    },
                }
            )
        if problems:
            kinds = sorted({problem for _, problem, _ in problems})
            if retry and positions == {0}:
                _log("devapi_call_arguments", outcome="invalid", reason=",".join(kinds))
                raise _Retry(correction(problems), frame)
            if "invalid_json" in kinds:
                _log("devapi_call_arguments", outcome="failed", reason="invalid_json")
                raise InvalidToolArguments(frame)
            # Valid JSON without a required argument: the client answers the model.
            _log("devapi_call_arguments", outcome="passed", reason="missing_required")
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
            positions.add(position)
            delta = dict(choice.get("delta") or {})
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
                    }
                    known[index] = current
                    held.setdefault(position, []).append(current)
                if new_id and not current["id"]:
                    current["id"] = new_id
                if function.get("name") and not current["name"]:
                    current["name"] = function["name"]
                current["arguments"] += function.get("arguments") or ""
            if choice.get("finish_reason") is not None:
                finished.append(position)
            if delta or choice.get("finish_reason") is not None:
                passed.append(dict(choice, delta=delta))
        # Held calls go out just before the frame that finishes their choice.
        for position in finished:
            complete = flush(position, frame)
            if complete is not None:
                last_out = monotonic()
                yield complete
        if passed or "usage" in frame:
            last_out = monotonic()
            yield dict(frame, choices=passed)
        elif not choices:
            last_out = monotonic()
            yield frame
        elif held and monotonic() - last_out >= KEEPALIVE_S:
            last_out = monotonic()
            yield _held_frame(min(held))
    # A stream that ended without finish_reason still delivers what it held.
    for position in list(held):
        complete = flush(position, None)
        if complete is not None:
            yield complete


def _decode_error(arguments: str) -> str:
    """Where the parser stopped, without quoting the arguments."""
    try:
        json.loads(arguments)
    except json.JSONDecodeError as exc:
        return f"{exc.msg} at character {exc.pos}"
    except (ValueError, RecursionError):
        return "not a JSON object"
    return "not a JSON object"
