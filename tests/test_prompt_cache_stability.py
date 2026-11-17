"""Agent conversations keep a byte-stable provider prompt prefix from turn to turn."""

import json
import os
from typing import Any
import unittest
from unittest.mock import patch

from services.orchestrator.dev_chat import RawChatInput
from services.orchestrator.dev_input import SemanticFieldError, messages_body
import dev_gateway as gateway

TOOLS = [
    {
        "name": f"tool_{index}",
        "description": f"Fictional tool number {index}.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
        },
    }
    for index in range(40)
]


def agent_request(
    turns: int, header: str, first_task: str = "Fix the tests."
) -> dict[str, Any]:
    """A coding-agent Messages request after ``turns`` tool calls, as a client sends it."""
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "<context>branch main</context>"},
                {"type": "text", "text": first_task},
            ],
        }
    ]
    for turn in range(turns):
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "plan", "signature": "sig"},
                    {
                        "type": "tool_use",
                        "id": f"call_{turn}",
                        "name": f"tool_{turn % 40}",
                        "input": {"path": f"src/file_{turn}.py", "limit": 200},
                    },
                ],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"call_{turn}",
                        "content": f"contents of file {turn}\n" * 50,
                        # Clients move their cache breakpoint to the newest block.
                        **(
                            {"cache_control": {"type": "ephemeral"}}
                            if turn == turns - 1
                            else {}
                        ),
                    }
                ],
            }
        )
    return {
        "model": "atlas-code",
        "max_tokens": 8192,
        "system": [
            {"type": "text", "text": header},
            {
                "type": "text",
                "text": "You are a fictional coding agent.\n" * 200,
                "cache_control": {"type": "ephemeral"},
            },
        ],
        "messages": messages,
        "tools": TOOLS,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
        "context_management": {
            "edits": [{"type": "clear_thinking_20251015", "keep": "all"}]
        },
        "metadata": {"user_id": "fictional-user"},
        "stream": True,
    }


def provider_body(value: dict[str, Any]) -> dict[str, Any]:
    converted = messages_body(value)
    for key in ("_hosted", "_fetch", "_code"):
        converted.pop(key, None)
    request = RawChatInput.model_validate(dict(converted, model="raw/public-routing"))
    plan = gateway.prepare_api(request, "atlas-code", "key-fictional")
    body: dict[str, Any] = json.loads(plan.body)
    return body


def prefix(body: dict[str, Any]) -> str:
    """Everything the provider reads before the conversation tail, serialized."""
    return json.dumps(
        {"tools": body.get("tools"), "messages": body["messages"]}, ensure_ascii=False
    )


class PromptCacheStabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(
            os.environ,
            {
                "SCW_GENERATIVE_BASE_URL": "https://provider.invalid/v1",
                "SCW_GENERATIVE_API_KEY": "test-provider-key",
                # Long agent prompts on the high route reserve more than the default cap.
                "ATLAS_RAW_CALL_MAX_MICRO_EUR": "1000000",
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def test_each_turn_extends_the_previous_provider_body(self) -> None:
        header = "x-client-billing-header: cc_version=9.9.9.abc; cc_entrypoint=cli;"
        bodies = [provider_body(agent_request(turns, header)) for turns in range(1, 16)]
        for earlier, later in zip(bodies, bodies[1:]):
            # Same tools, and the earlier messages unchanged at the start.
            self.assertEqual(earlier["tools"], later["tools"])
            self.assertEqual(
                later["messages"][: len(earlier["messages"])], earlier["messages"]
            )
            self.assertEqual(earlier["prompt_cache_key"], later["prompt_cache_key"])
            serialized = json.dumps(later["messages"], ensure_ascii=False)
            self.assertTrue(
                serialized.startswith(
                    json.dumps(earlier["messages"], ensure_ascii=False)[:-1]
                )
            )

    def test_client_header_block_is_not_part_of_the_prompt(self) -> None:
        # A client may put a per-request value in a header line at the start of the
        # system prompt: every request would then start with different bytes.
        first = provider_body(
            agent_request(3, "x-client-billing-header: cc_version=1; cch=a1b2c;")
        )
        second = provider_body(
            agent_request(3, "x-client-billing-header: cc_version=1; cch=f9e8d;")
        )
        self.assertEqual(prefix(first), prefix(second))
        self.assertEqual(first["prompt_cache_key"], second["prompt_cache_key"])
        self.assertNotIn("billing-header", first["messages"][0]["content"])
        self.assertTrue(
            first["messages"][0]["content"].startswith("You are a fictional")
        )

    def test_header_like_text_inside_instructions_is_kept(self) -> None:
        value = agent_request(1, "Header notes")
        value["system"].append(
            {"type": "text", "text": "x-request-header: keep\nUse it in every call."}
        )
        system = messages_body(value)["messages"][0]["content"]
        self.assertIn("x-request-header: keep", system)
        value["system"] = [{"type": "text", "text": "x-client-billing-header: v=1;"}]
        self.assertEqual(messages_body(value)["messages"][0]["role"], "user")

    def test_one_cache_key_per_conversation(self) -> None:
        header = "x-client-billing-header: cc_version=1;"
        one = provider_body(agent_request(4, header, "Fix the tests."))
        other = provider_body(agent_request(4, header, "Write the docs."))
        self.assertNotEqual(one["prompt_cache_key"], other["prompt_cache_key"])
        third = provider_body(agent_request(9, header, "Fix the tests."))
        self.assertEqual(one["prompt_cache_key"], third["prompt_cache_key"])
        key_text = json.dumps(one)
        # The key is a digest: no conversation text and no API key identifier in it.
        self.assertRegex(one["prompt_cache_key"], "^[0-9a-f]{64}$")
        self.assertNotIn("key-fictional", one["prompt_cache_key"])
        self.assertIn("prompt_cache_key", key_text)

    def test_context_edits_are_accepted_or_refused_by_name(self) -> None:
        value = agent_request(1, "x-client-billing-header: v=1;")
        self.assertEqual(messages_body(value)["reasoning_effort"], "high")
        value["context_management"] = {"edits": [{"type": "clear_tool_uses_20250919"}]}
        with self.assertRaisesRegex(SemanticFieldError, "clear_tool_uses_20250919"):
            messages_body(value)
        value["context_management"] = {"edits": "all"}
        with self.assertRaises(ValueError):
            messages_body(value)


if __name__ == "__main__":
    unittest.main()
