"""Owner report 2026-11-16: phone sidebar toggle too small, answer headings too large on phones.

Static guard on services/orchestrator/chat-ui.css; tests/phone_header_headings_browser.py measures
the rendered result on Open WebUI 0.11 and 0.13.
"""

from pathlib import Path
import re
import unittest

CSS = Path("services/orchestrator/chat-ui.css").read_text()


def media_block(query: str) -> str:
    """Concatenate the bodies of every @media block with this exact query."""
    bodies = []
    for match in re.finditer(re.escape(f"@media ({query})") + r"\s*\{", CSS):
        depth, start = 1, match.end()
        for index in range(start, len(CSS)):
            depth += {"{": 1, "}": -1}.get(CSS[index], 0)
            if depth == 0:
                bodies.append(CSS[start:index])
                break
    return "\n".join(bodies)


def declarations(block: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", block)
    assert match, selector
    return match.group(1)


class PhoneHeaderToggleTests(unittest.TestCase):
    def test_navbar_toggle_has_a_44_px_target_and_24_px_icon(self) -> None:
        # Open WebUI draws the navbar toggle below 768 px only, in 0.11 and 0.13 alike.
        phone = media_block("max-width:767px")
        button = declarations(phone, "#sidebar-toggle-button")
        self.assertIn("min-width:44px", button)
        self.assertIn("min-height:44px", button)
        self.assertIn("color:var(--atlas-text)!important", button)
        # Negative block margins keep the header row height.
        self.assertIn("margin:-8px 0 -8px -8px", button)
        self.assertIn(
            "padding:0!important", declarations(phone, "#sidebar-toggle-button > div")
        )
        icon = declarations(phone, "#sidebar-toggle-button svg")
        self.assertIn("width:24px!important", icon)
        self.assertIn("height:24px!important", icon)


class PhoneHeadingTests(unittest.TestCase):
    def test_phone_heading_scale(self) -> None:
        phone = media_block("max-width:640px")
        for tag, scale in (("h1", "1.25"), ("h2", "1.125"), ("h3", "1.0625")):
            rule = declarations(
                phone, f":is(.chat-assistant,.user-message) .markdown-prose {tag}"
            )
            self.assertIn(
                f"font-size:calc(var(--atlas-body-size) * {scale})!important", rule
            )

    def test_desktop_scale_is_kept_and_headings_use_a_title_leading(self) -> None:
        for tag, scale in (("h1", "1.75"), ("h2", "1.375"), ("h3", "1.125")):
            self.assertIn(
                f".chat-assistant .markdown-prose {tag} {{ font-size:calc(var(--atlas-body-size) * {scale})!important; }}",
                CSS,
            )
        for scope in (".chat-assistant", ".user-message"):
            rule = declarations(
                CSS, f"html:root body {scope} .markdown-prose :is(h1,h2,h3,h4,h5,h6)"
            )
            self.assertIn("line-height:1.3!important", rule)


if __name__ == "__main__":
    unittest.main()
