"""Stateless history translations preserve user code and reject unsupported features."""

import json
from typing import Any
import unittest
from services.orchestrator.dev_input import responses, messages


class DevInputTests(unittest.TestCase):
    def test_responses_history_functions_and_metadata(self) -> None:
        payload: dict[str, Any] = {
            "model": "atlas-code",
            "instructions": "Read code.",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "héllo"}],
                },
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "read",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "c1", "output": "code"},
            ],
            "tools": [
                {"type": "function", "name": "read", "parameters": {"type": "object"}}
            ],
            "tool_choice": {"type": "function", "name": "read"},
            "store": False,
            "reasoning": {"effort": "none"},
            "client_metadata": {"user_id": "untrusted"},
        }
        request = responses(payload)
        self.assertEqual(request.messages[1].content, "héllo")
        self.assertEqual(request.messages[-1].tool_call_id, "c1")
        self.assertEqual(request.messages[-1].content, "code")
        self.assertNotIn("untrusted", request.model_dump_json())
        extra: dict[str, Any]
        for extra in (
            {"store": True},
            {"previous_response_id": "private"},
            {"include": ["other"]},
        ):
            with self.assertRaises(ValueError):
                responses(payload | extra)
        self.assertEqual(
            responses({"model": "atlas-code", "input": "short"}).messages[0].content,
            "short",
        )
        self.assertEqual(
            responses(payload | {"reasoning": {"summary": "auto"}}).reasoning_effort,
            "none",
        )

    def test_responses_drops_unknown_tools_and_invalid_namespaces(self) -> None:
        payload: dict[str, Any] = {
            "model": "atlas-code",
            "input": "fictional",
            "tools": [{"type": "web_search", "fictional_option": True}],
        }
        self.assertEqual(responses(payload).tools, [])
        self.assertEqual(
            responses(
                payload
                | {
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "multi_agent_v1",
                            "tools": [{"type": "custom", "name": "not_a_function"}],
                        }
                    ]
                }
            ).tools,
            [],
        )

    def test_messages_history_errors_and_cache_hints(self) -> None:
        payload: dict[str, Any] = {
            "model": "atlas-code",
            "max_tokens": 512,
            "system": [
                {
                    "type": "text",
                    "text": "Read code.",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "c1", "name": "read", "input": {}}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "c1",
                            "content": [{"type": "text", "text": "missing"}],
                            "is_error": True,
                        }
                    ],
                },
            ],
            "tools": [{"name": "read", "input_schema": {"type": "object"}}],
            "tool_choice": {
                "type": "tool",
                "name": "read",
                "disable_parallel_tool_use": True,
            },
            "output_config": {"effort": "high"},
        }
        request = messages(payload)
        self.assertEqual(
            json.loads(request.messages[-1].content or ""), {"tool_error": "missing"}
        )
        self.assertFalse(request.parallel_tool_calls)
        self.assertEqual(request.reasoning_effort, "none")
        extra: dict[str, Any]
        for extra in (
            {"thinking": {"type": "adaptive"}},
            {"output_config": {"format": {}}},
            {"metadata": {"tenant": "outside"}},
            {"system": [{"type": "image"}]},
            {"tool_choice": {"type": "other"}},
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_use", "id": "c", "name": "read", "input": {}}
                        ],
                    }
                ]
            },
        ):
            with self.assertRaises(ValueError):
                messages(payload | extra)
        self.assertEqual(
            messages(
                {
                    "model": "atlas-code",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                }
            )
            .messages[0]
            .content,
            "hi",
        )
