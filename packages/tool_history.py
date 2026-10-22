"""Lossless answer-only context without executable historical tool-call state."""

import copy
import json


def final_messages(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    normalized = copy.deepcopy(messages)
    for index, message in enumerate(messages):
        if message.get("role") == "tool" or message.get("tool_calls"):
            # Evidence stays outside system/user instructions. JSON retains IDs,
            # arguments, full source text and provenance without delimiter parsing.
            normalized[index] = {
                "role": "assistant",
                "content": json.dumps(
                    {"untrusted_tool_history": message}, ensure_ascii=False
                ),
            }
    if any(
        message.get("role") == "tool" or message.get("tool_calls")
        for message in messages
    ):
        question = next(
            (
                message.get("content")
                for message in reversed(messages)
                if message.get("role") == "user"
            ),
            None,
        )
        if isinstance(question, list):
            question = "\n".join(
                str(part.get("text", ""))
                for part in question
                if isinstance(part, dict) and part.get("type") == "text"
            )
        if isinstance(question, str) and question.strip():
            # Repeat only the user's own request, never promote source evidence.
            # Original multimodal inputs and every historical byte remain above.
            normalized.append({"role": "user", "content": question})
    return normalized


def is_history_answer(text: str) -> bool:
    """Recognize an echoed transport envelope, not ordinary JSON/code answers."""
    value: object = text.strip()
    if isinstance(value, str) and value.startswith("```") and value.endswith("```"):
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    for _ in range(2):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            # A corrupt envelope is still a transport artifact, not user content.
            normalized = "".join(text.split()).replace(chr(92), "")
            return (
                possible_history_answer(text)
                and '"untrusted_tool_history":' in normalized
            )
    return (
        isinstance(value, dict)
        and set(value) == {"untrusted_tool_history"}
        and isinstance(value["untrusted_tool_history"], dict)
        and value["untrusted_tool_history"].get("role") in ("tool", "assistant")
    )


def possible_history_answer(text: str) -> bool:
    """Hold only a possible transport envelope while its prefix is arriving."""
    value = "".join(text.split()).replace('\\"', '"')
    if value.startswith("```"):
        value = value[3:]
        if "json".startswith(value):
            return True
        if value.startswith("json"):
            value = value[4:]
    elif "```".startswith(value):
        return True
    if value.startswith('"'):
        value = value[1:]
    value = value.rstrip(chr(92))
    marker = '{"untrusted_tool_history":'
    return marker.startswith(value) or value.startswith(marker)
