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
        # Codex grows a turn description in client_metadata during long sessions.
        long_turn = {"x-codex-turn-metadata": "t" * 4096}
        self.assertNotIn(
            "tttt",
            responses(payload | {"client_metadata": long_turn}).model_dump_json(),
        )
        with self.assertRaises(ValueError):
            responses(payload | {"metadata": {"note": "m" * 1025}})
        with self.assertRaises(ValueError):
            responses(payload | {"client_metadata": {"turn": "t" * 65537}})
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

    def test_long_agent_history_fits_the_raw_message_limit(self) -> None:
        # Measured 2026-10-30: a Codex session of about 1250 small tool turns was
        # refused with "measured length 1025; max_length 1024" on the message list.
        from services.orchestrator.dev_chat import RawChatInput

        items: list[dict[str, Any]] = [
            {"type": "message", "role": "user", "content": "Start."}
        ]
        for index in range(1500):
            items += [
                {
                    "type": "function_call",
                    "call_id": f"c{index}",
                    "name": "read",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": f"c{index}",
                    "output": "ok",
                },
            ]
        tools = [{"type": "function", "name": "read", "parameters": {"type": "object"}}]
        parsed = responses({"model": "atlas-code", "input": items, "tools": tools})
        raw = RawChatInput.model_validate(
            parsed.model_dump(by_alias=True, exclude_none=True)
            | {"model": "raw/public-routing"}
        )
        self.assertGreater(len(raw.messages), 1024)

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


class NullOptionalFieldTests(unittest.TestCase):
    def test_null_optional_fields_are_absent(self) -> None:
        # Shape sent by Codex CLI 0.98: unset optional fields arrive as null.
        chat = responses(
            {
                "model": "atlas-code",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Fictional task."}],
                    }
                ],
                "reasoning": None,
                "prompt_cache_key": None,
                "tool_choice": "auto",
                "tools": [
                    {
                        "type": "function",
                        "name": "exec_command",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            }
        )
        self.assertEqual([t.function.name for t in chat.tools], ["exec_command"])
