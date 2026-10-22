"""M7 request constraints are enforced before any provider invocation."""

import unittest
from services.orchestrator.chat_schema import ChatRequest


class ChatSchemaTests(unittest.TestCase):
    def test_request_limits_and_history(self) -> None:
        messages = [
            {"role": "user", "content": "Bonjour"},
            {"role": "assistant", "content": "Salut"},
            {"role": "user", "content": "Traduis cela en allemand."},
        ]
        req = ChatRequest.model_validate(
            {
                "model": "atlas-qwen",
                "messages": messages,
                "stream": True,
                "temperature": 0.2,
            }
        )
        self.assertEqual(len(req.messages), 3)
        self.assertTrue(req.stream)
        for content in ("", " ", "a" * 1100000, "control\x00", "\ud800"):
            with (
                self.subTest(content=repr(content[:20])),
                self.assertRaises(ValueError),
            ):
                ChatRequest.model_validate(
                    {"messages": [{"role": "user", "content": content}]}
                )
        # M25 replaces historical character caps with the active token window.
        for length in (32001, 40000):
            accepted = ChatRequest.model_validate(
                {"messages": [{"role": "user", "content": "a" * length}]}
            )
            self.assertEqual(len(accepted.messages[0].text), length)
        payloads: tuple[dict[str, object], ...] = (
            {"messages": []},
            {"messages": [messages[0], {"role": "assistant", "content": ""}]},
            {"model": "unknown", "messages": messages},
            {"messages": [{"role": "tool", "content": "unsafe"}]},
            {"messages": [{"role": "user", "content": "x" * 600000}] * 2},
        )
        for payload in payloads:
            with self.assertRaises(ValueError):
                ChatRequest.model_validate(payload)
        # A conversation ending on a non-empty assistant message is a continuation request.
        self.assertEqual(
            len(ChatRequest.model_validate({"messages": messages[:2]}).messages), 3
        )

    def test_public_output_reservation(self) -> None:
        for profile in ("atlas-qwen", "atlas-glm", "atlas-deepseek"):
            payload = {
                "model": profile,
                "messages": [{"role": "user", "content": "Explain"}],
            }
            self.assertEqual(ChatRequest.model_validate(payload).max_tokens, 6000)
            self.assertEqual(
                ChatRequest.model_validate({**payload, "max_tokens": 6000}).max_tokens,
                6000,
            )
            with self.assertRaisesRegex(ValueError, "6001.*6000"):
                ChatRequest.model_validate({**payload, "max_tokens": 6001})
