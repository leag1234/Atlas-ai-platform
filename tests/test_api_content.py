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
