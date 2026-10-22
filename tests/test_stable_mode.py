"""Offline stable-mode policy coverage."""

import unittest

from services.orchestrator.chat_pipeline import without_model_code_tools


class StableModeTests(unittest.TestCase):
    def test_model_code_tools_are_removed_but_read_only_tools_remain(self) -> None:
        tools: list[dict[str, object]] = [
            {"type": "function", "function": {"name": "web_search"}},
            {"type": "function", "function": {"name": "terminal_command"}},
            {"type": "function", "function": {"name": "publish_document"}},
        ]
        self.assertEqual(
            without_model_code_tools(tools),
            [{"type": "function", "function": {"name": "web_search"}}],
        )
