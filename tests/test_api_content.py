"""Assistant names and attached files on the direct API routes."""

import base64
import unittest
from typing import Any

from packages.api_content import FileContentError, assistant_name, inline_files
from packages.profiles import PROFILES


class ApiContentTests(unittest.TestCase):
    def test_assistant_names(self) -> None:
        self.assertTrue(assistant_name("atlas"))
        for name in PROFILES:
            self.assertTrue(assistant_name(name))
        self.assertFalse(assistant_name("atlas-dev"))
        self.assertFalse(assistant_name("atlas-chat"))

    def test_file_part_becomes_its_text(self) -> None:
        data = base64.b64encode("Budget: 4 000 euros.".encode()).decode()
        messages: list[Any] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is the budget?"},
                    {
                        "type": "file",
                        "file": {"filename": "notes.txt", "file_data": data},
                    },
                ],
            }
        ]
        inline_files(messages)
        part = messages[0]["content"][1]
        self.assertEqual(part["type"], "text")
        self.assertIn("Attached file notes.txt", part["text"])
        self.assertIn("4 000 euros", part["text"])
        self.assertEqual(messages[0]["content"][0]["text"], "What is the budget?")

    def test_data_url_and_bad_files(self) -> None:
        data = "data:text/plain;base64," + base64.b64encode(b"hello").decode()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "file", "file": {"filename": "a.txt", "file_data": data}}
                ],
            }
        ]
        self.assertIn("hello", inline_files(messages)[0]["content"][0]["text"])
        for file in (
            {},
            {"filename": "a.txt", "file_data": "%%%"},
            {"filename": "a.txt", "file_data": ""},
        ):
            with self.subTest(file=file):
                with self.assertRaises(FileContentError):
                    inline_files(
                        [{"role": "user", "content": [{"type": "file", "file": file}]}]
                    )

    def test_messages_without_files_are_unchanged(self) -> None:
        messages: list[Any] = [
            {"role": "user", "content": "plain"},
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "x"}}],
            },
        ]
        self.assertEqual(inline_files([dict(m) for m in messages]), messages)


class ClientExtrasTests(unittest.TestCase):
    """opencode and other OpenAI-compatible clients send optional fields the direct models ignore."""

    def opencode_body(self) -> dict[str, Any]:
        return {
            "model": "raw/public-routing",
            "stream": True,
            "stream_options": {"include_usage": True},
            "prompt_cache_key": "session-1",
            "store": False,
            "top_p": 1,
            "max_tokens": 1000,
            "parallel_tool_calls": True,
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "read", "parameters": {"type": "object"}},
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": "You are a coding agent.",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                },
                {"role": "user", "content": "Read main.py"},
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "I should read the file.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "print(1)"},
            ],
        }

    def test_opencode_request_validates(self) -> None:
        from packages.api_content import drop_client_extras
        from services.orchestrator.dev_chat import RawChatInput

        parsed = RawChatInput.model_validate(drop_client_extras(self.opencode_body()))
        self.assertEqual(len(parsed.messages), 4)
        self.assertEqual(parsed.tools[0].function.name, "read")

    def test_unknown_field_is_named(self) -> None:
        from pydantic import ValidationError

        from packages.api_content import drop_client_extras
        from packages.validation import describe_validation
        from services.orchestrator.dev_chat import RawChatInput

        body = drop_client_extras(dict(self.opencode_body(), invented_option=1))
        with self.assertRaises(ValidationError) as caught:
            RawChatInput.model_validate(body)
        self.assertIn(
            "unknown field invented_option", describe_validation(caught.exception)
        )
