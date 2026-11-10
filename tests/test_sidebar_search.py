"""Owner report 2026-11-10: sidebar search (Ctrl+K) must open the matching chat
and Enter in the search box must never submit the composer."""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAT_UI = (ROOT / "services/orchestrator/chat-ui.js").read_text()
CHAT_CSS = (ROOT / "services/orchestrator/chat-ui.css").read_text()


def handler_block(source: str) -> str:
    start = source.index("// Sidebar search (Ctrl+K")
    end = source.index("// Owner Code workspace", start)
    return source[start:end]


class SidebarSearchHandlerTests(unittest.TestCase):
    def test_keydown_guard_requires_a_dialog_and_an_input(self) -> None:
        block = handler_block(CHAT_UI)
        self.assertIn('event.key !== "Enter"', block)
        self.assertIn('target.closest("dialog")', block)
        self.assertIn("event.preventDefault()", block)
        self.assertIn("stopImmediatePropagation()", block)
        # The guard is registered in the capture phase so it runs before the host.
        self.assertRegex(
            block, re.compile(r'addEventListener\("keydown".*true\)', re.S)
        )

    def test_submit_guard_only_fires_while_the_search_dialog_is_open(self) -> None:
        block = handler_block(CHAT_UI)
        self.assertIn('addEventListener("submit"', block)
        self.assertIn("dialog[open] input[type='search']", block)
        self.assertIn("event.preventDefault()", block)

    def test_handler_is_scoped_to_the_search_dialog_not_any_input(self) -> None:
        block = handler_block(CHAT_UI)
        # The condition must require a dialog ancestor, so project/code inputs are untouched.
        self.assertIn("inSearchDialog", block)
        self.assertIn('closest("dialog")', block)

    def test_handler_runs_in_node_without_a_dom(self) -> None:
        block = handler_block(CHAT_UI)
        program = (
            "const listeners = [];\n"
            "const document = { addEventListener: (t, f, c) => listeners.push([t, f, c]) };\n"
            "const Element = class {};\n"
            + block
            + "\nlisteners.forEach(([t]) => { if (t !== 'keydown' && t !== 'submit') throw new Error(t); });\n"
            "console.log('ok');\n"
        )
        result = subprocess.run(
            ["node", "-e", program], capture_output=True, text=True, check=True
        )
        self.assertIn("ok", result.stdout)


class SidebarSearchLayoutTests(unittest.TestCase):
    def test_search_dialog_is_bounded_and_centered(self) -> None:
        self.assertIn("max-width:calc(100vw - 32px)", CHAT_CSS)
        self.assertIn("max-height:calc(100dvh - 32px)", CHAT_CSS)
        self.assertIn("margin:auto", CHAT_CSS)
        self.assertIn("overflow:auto", CHAT_CSS)

    def test_search_input_cannot_overflow_the_panel(self) -> None:
        self.assertIn("width:100%", CHAT_CSS)
        self.assertIn("min-width:0", CHAT_CSS)
        self.assertIn("box-sizing:border-box", CHAT_CSS)

    def test_new_rules_carry_explanatory_comments(self) -> None:
        self.assertIn(
            "Sidebar search dialog (Ctrl+K) stays inside the viewport", CHAT_CSS
        )
        self.assertIn(
            "The search input keeps its full width and never overflows", CHAT_CSS
        )


if __name__ == "__main__":
    unittest.main()
