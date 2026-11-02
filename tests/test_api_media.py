"""Images and documents on the Responses and Messages routes of the direct models."""

import base64
import unittest
from typing import Any

from packages.api_content import inline_files, normalize_chat
from services.orchestrator.dev_chat import ChatInput, RawChatInput
from services.orchestrator.dev_input import messages_body, responses_body, text_only

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVQI12P4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
TEXT = base64.b64encode(b"Budget: 4000 euros.").decode()


def routed(body: dict[str, Any]) -> RawChatInput:
    ChatInput.model_validate(text_only(body))
    normalize_chat(body)
    inline_files(body["messages"])
    return RawChatInput.model_validate(dict(body, model="raw/public-routing"))


def parts(parsed: RawChatInput, index: int = -1) -> list[dict[str, Any]]:
    content = parsed.messages[index].content
    assert isinstance(content, list)
    return content


class MessagesMediaTests(unittest.TestCase):
    def body(self, *blocks: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return messages_body(
            {
                "model": "atlas-code",
                "max_tokens": 32,
                "messages": [
                    {
                        "role": "user",
                        "content": [*blocks, {"type": "text", "text": "What is it?"}],
                    }
                ],
                **extra,
            }
        )

    def test_image_block_becomes_an_image_part(self) -> None:
        image = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": PNG},
        }
        content = parts(routed(self.body(image)))
        self.assertEqual(content[0]["type"], "image_url")
        self.assertTrue(
            content[0]["image_url"]["url"].startswith("data:image/png;base64,")
        )
        self.assertEqual(content[1], {"type": "text", "text": "What is it?"})

    def test_documents_and_search_results_become_text(self) -> None:
        blocks: list[dict[str, Any]] = [
            {
                "type": "document",
                "title": "minutes",
                "source": {
                    "type": "text",
                    "media_type": "text/plain",
                    "data": "Budget: 4000 euros.",
                },
            },
            {
                "type": "document",
                "title": "notes.txt",
                "source": {"type": "base64", "media_type": "text/plain", "data": TEXT},
            },
            {
                "type": "search_result",
                "source": "https://example.invalid",
                "title": "Result",
                "content": [{"type": "text", "text": "4000"}],
            },
        ]
        content = parts(routed(self.body(*blocks)), 0)
        text = "\n".join(part["text"] for part in content)
        self.assertIn("Document minutes:\nBudget: 4000 euros.", str(text))
        self.assertIn("Attached file notes.txt:", str(text))
        self.assertIn(
            "Search result Result (https://example.invalid):\n4000", str(text)
        )

    def test_thinking_of_an_earlier_turn_is_dropped(self) -> None:
        body = messages_body(
            {
                "model": "atlas-code",
                "max_tokens": 32,
                "messages": [
                    {"role": "user", "content": "Hi"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "x", "signature": "s"},
                            {"type": "text", "text": "Hello"},
                        ],
                    },
                    {"role": "user", "content": "Again"},
                ],
            }
        )
        self.assertEqual(routed(body).messages[1].content, "Hello")

    def test_text_only_requests_are_unchanged(self) -> None:
        parsed = routed(
            messages_body(
                {
                    "model": "atlas-code",
                    "max_tokens": 32,
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "Hi"}]}
                    ],
                }
            )
        )
        self.assertEqual(parsed.messages[0].content, "Hi")


class ResponsesMediaTests(unittest.TestCase):
    def test_input_image_and_input_file(self) -> None:
        body = responses_body(
            {
                "model": "atlas-code",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "What is the budget?"},
                            {
                                "type": "input_image",
                                "image_url": "data:image/png;base64," + PNG,
                                "detail": "low",
                            },
                            {
                                "type": "input_file",
                                "filename": "notes.txt",
                                "file_data": "data:text/plain;base64," + TEXT,
                            },
                        ],
                    }
                ],
            }
        )
        content = parts(routed(body))
        self.assertEqual([p["type"] for p in content], ["text", "image_url", "text"])
        self.assertEqual(content[1]["image_url"]["detail"], "low")
        self.assertIn("Budget: 4000 euros.", content[2]["text"])

    def test_developer_input_is_a_system_message(self) -> None:
        body = responses_body(
            {
                "model": "atlas-code",
                "input": [
                    {"role": "developer", "content": "Be brief."},
                    {"role": "user", "content": "Hi"},
                ],
            }
        )
        self.assertEqual(routed(body).messages[0].role, "system")

    def test_file_reference_needs_the_files_api(self) -> None:
        with self.assertRaises(ValueError):
            responses_body(
                {
                    "model": "atlas-code",
                    "input": [
                        {
                            "role": "user",
                            "content": [{"type": "input_image", "file_id": "file_1"}],
                        }
                    ],
                }
            )
