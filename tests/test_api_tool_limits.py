"""Client agents send long tool descriptions and many tools (2026-11-17)."""

import unittest

from pydantic import ValidationError

from packages.dev_request import MAX_TOOLS, TOOL_DESCRIPTION_CHARS, Request
from services.orchestrator.dev_chat import ChatInput, RawChatInput


def tool(index: int, description: str = "Does one thing.") -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": f"tool_{index}",
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def body(tools: list[dict[str, object]], model: str = "") -> dict[str, object]:
    request: dict[str, object] = {
        "messages": [{"role": "user", "content": "Hello"}],
        "tools": tools,
    }
    if model:
        request["model"] = model
    return request


class ToolLimitTests(unittest.TestCase):
    def test_a_long_agent_tool_description_is_accepted(self) -> None:
        long_text = "Run several steps in order and report each result. " * 400
        self.assertGreater(len(long_text), 18_780)
        tools = [tool(0, long_text)] + [tool(i) for i in range(1, 40)]
        Request.model_validate(body(tools))
        ChatInput.model_validate(body(tools, "atlas-code"))
        RawChatInput.model_validate(body(tools, "raw/provider-model"))

    def test_up_to_the_documented_tool_count_is_accepted(self) -> None:
        Request.model_validate(body([tool(i) for i in range(MAX_TOOLS)]))
        RawChatInput.model_validate(
            body([tool(i) for i in range(MAX_TOOLS)], "raw/provider-model")
        )

    def test_past_the_documented_limits_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            Request.model_validate(body([tool(i) for i in range(MAX_TOOLS + 1)]))
        with self.assertRaises(ValidationError):
            Request.model_validate(body([tool(0, "x" * (TOOL_DESCRIPTION_CHARS + 1))]))


if __name__ == "__main__":
    unittest.main()
