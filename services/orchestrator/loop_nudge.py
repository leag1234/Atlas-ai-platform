"""Nudge a coding agent out of a tool-call loop (owner request 2026-10-31).

The API is stateless, so the rule reads the history alone. When the newest tool call has
the same name and arguments as at least ``REPEATS - 1`` earlier calls among the last
``WINDOW`` calls, and all of them returned the same output (timing lines ignored), a short
note is appended to the newest tool output asking the model to change approach. Measured
on 2026-10-31: a coding worker received the same empty search result 472 times and
reran the search each time. Repeated calls whose outputs differ (tests rerun after an
edit, a build that progresses) are never touched."""

import re
from typing import Any

WINDOW = 12
REPEATS = 3
NOTE = (
    "\n\n[Atlas note: this exact call ({name}) has now returned the same result {count} "
    "times in this session. Do not run it again with the same arguments. Use what you "
    "already know, try a different approach, or stop and report what blocks you.]"
)
REPLACE_AT = 5
# Every one of the last WINDOW calls identical with the same result: refuse the request.
LOOP_STOP = WINDOW
REPLACEMENT = (
    "[Atlas note: this exact call ({name}) has returned this same result {count} times; "
    "the result is already earlier in this conversation, so it is not repeated here. "
    "Do not run this call again. Take the next step of your task with what you already "
    "know, run a different command, or stop and report what blocks you.]"
)
TIMING = re.compile(r"^(?:Chunk ID: \S+|Wall time: [\d.]+ seconds?)\n", re.MULTILINE)


def normalized(output: str | None) -> str:
    return TIMING.sub("", output or "")


def nudge_messages(messages: list[Any]) -> tuple[list[Any], int]:
    """Return the messages, with a note on the last tool output when the agent loops,
    and the repeat count that triggered it (0 when nothing changed)."""
    calls = [
        (call.id, call.function.name, call.function.arguments)
        for message in messages
        if message.role == "assistant"
        for call in message.tool_calls
    ]
    if len(calls) < REPEATS:
        return messages, 0
    outputs = {
        message.tool_call_id: (index, message.content)
        for index, message in enumerate(messages)
        if message.role == "tool" and message.tool_call_id
    }
    last_id, name, arguments = calls[-1]
    if last_id not in outputs:
        return messages, 0
    index, last_output = outputs[last_id]
    same = [
        call_id
        for call_id, call_name, call_arguments in calls[-WINDOW:]
        if (call_name, call_arguments) == (name, arguments)
        and call_id in outputs
        and normalized(outputs[call_id][1]) == normalized(last_output)
    ]
    if len(same) < REPEATS:
        return messages, 0
    # Measured 2026-10-31: a model ignored the appended note about 20 times in a row;
    # from REPLACE_AT repeats the duplicate output is replaced by the note alone.
    content = (
        REPLACEMENT.format(name=name, count=len(same))
        if len(same) >= REPLACE_AT
        else (last_output or "") + NOTE.format(name=name, count=len(same))
    )
    result = list(messages)
    result[index] = messages[index].model_copy(update={"content": content})
    return result, len(same)
