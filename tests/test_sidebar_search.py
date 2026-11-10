"""Sidebar search: Enter opens the first found chat, not a new conversation (owner report 2026-11-10)."""

import unittest
from pathlib import Path

JS = Path("services/orchestrator/chat-ui.js").read_text()
CSS = Path("services/orchestrator/chat-ui.css").read_text()


class SidebarSearchTests(unittest.TestCase):
    def block(self) -> str:
        start = JS.index("// Sidebar search (Ctrl+K, owner report 2026-11-10)")
        return JS[start : JS.index("// Owner Code workspace", start)]

    def test_enter_opens_the_first_chat_result_only_when_there_is_one(self) -> None:
        block = self.block()
        self.assertIn("a[href^='/c/']", block)
        self.assertIn("if (!first) return;", block)
        self.assertIn("first.click()", block)
        self.assertIn('closest("[role=dialog], dialog")', block)

    def test_empty_query_and_composition_keep_native_enter(self) -> None:
        block = self.block()
        self.assertIn("event.isComposing", block)
        self.assertIn("input.value.trim()", block)

    def test_focus_ring_stays_inside_the_dialog(self) -> None:
        self.assertIn("input[placeholder]:focus { outline:none", CSS)
        self.assertIn("box-shadow:inset 0 0 0 1px var(--atlas-accent)", CSS)
