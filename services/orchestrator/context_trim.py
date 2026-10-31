"""Server-side trimming of old tool outputs for long coding-agent sessions.

Opt-in per request (header ``X-Atlas-Context-Trim: on``). The API is stateless, so the
rule depends on the history alone: the newest ``KEEP`` tool outputs stay whole, and older
ones are replaced by a short stub in whole blocks of ``BLOCK`` outputs. The prompt prefix
therefore changes only once every ``BLOCK`` tool calls, which keeps the provider's prompt
cache warm between two trimming points. Measured offline on two recorded Codex sessions
(1068 and 1247 turns): input cost divided by about 4 with about 99 percent cached."""

from typing import Any

KEEP = 10
BLOCK = 20
STUB = (
    "[Atlas removed this older tool output ({size} characters) to keep the context "
    "small. Run the tool again if you need it.]"
)


def trimmed_count(tool_outputs: int) -> int:
    """Number of oldest tool outputs to replace, in whole blocks."""
    return max(0, (tool_outputs - KEEP) // BLOCK * BLOCK)


def trim_messages(messages: list[Any]) -> list[Any]:
    """Return messages with the oldest tool outputs replaced by a stub."""
    tools = [index for index, message in enumerate(messages) if message.role == "tool"]
    count = trimmed_count(len(tools))
    if count == 0:
        return messages
    replaced = set(tools[:count])
    result = []
    for index, message in enumerate(messages):
        if index in replaced:
            size = len(message.content) if isinstance(message.content, str) else 0
            message = message.model_copy(update={"content": STUB.format(size=size)})
        result.append(message)
    return result
