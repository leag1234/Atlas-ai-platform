"""Static contracts for the sidebar project rows, the move menu, long user messages
and imported drawings in the project view (owner report 2026-11-13)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ORCHESTRATOR = Path("services/orchestrator")


def read(name: str) -> str:
    return (ORCHESTRATOR / name).read_text()


class MoveMenuSurfaceTests(unittest.TestCase):
    def test_project_tokens_exist_on_the_root_for_body_level_surfaces(self) -> None:
        css = read("project_mode.css")
        match = re.search(r"^html \{ (--project-bg:[^}]*)\}", css, re.M)
        self.assertIsNotNone(match, "the project tokens must be defined on the root")
        declarations = match.group(1) if match else ""
        for token in ("bg", "card", "line", "ink", "muted", "accent", "hover"):
            self.assertIn(f"--project-{token}:var(--atlas-", declarations)

    def test_move_menu_heading_never_shares_the_scrolling_list(self) -> None:
        css = read("project_mode.css")
        self.assertIn(
            ".atlas-project-move-menu { display:flex; flex-direction:column;", css
        )
        self.assertIn(".atlas-project-move-menu h2 { flex:none;", css)
        self.assertIn(
            ".atlas-project-move-menu .atlas-project-move-form { flex:1 1 auto; min-height:0; overflow:auto;",
            css,
        )


class SidebarRowTests(unittest.TestCase):
    def test_selected_rows_share_one_token_pair(self) -> None:
        chat = read("chat-ui.css")
        project = read("project_mode.css")
        self.assertIn(
            "#sidebar a.selected { background:var(--atlas-row-selected-bg)!important; color:var(--atlas-row-selected-fg)!important; }",
            chat,
        )
        self.assertIn(
            'html[data-atlas-style="dense"] { --atlas-row-selected-bg:var(--atlas-border); --atlas-row-selected-fg:var(--atlas-text); }',
            chat,
        )
        self.assertNotIn(".atlas-project-host-conversation[aria-current=page]", project)
        self.assertIn(
            ".atlas-project-host-projects .atlas-project-sidebar-row[aria-current=page] { background:var(--atlas-row-selected-bg,var(--atlas-accent))!important; color:var(--atlas-row-selected-fg,var(--atlas-on-accent))!important;",
            project,
        )

    def test_project_rows_have_a_menu_with_the_existing_project_actions(self) -> None:
        js = read("project_mode.js")
        self.assertIn("item.append(rowMenuButton(value.name, actions))", js)
        for action in ("'rename'", "'archive'", "'deleteProject'"):
            self.assertIn(action, js[js.index("function sidebarProjectActions") :])
        self.assertIn("aria-haspopup', 'menu'", js)
        # The open project with a conversation open is a location, not a second selected row.
        self.assertIn(
            "row.setAttribute('aria-current', scope.conversation ? 'true' : 'page')", js
        )
        # The "..." control must not close the phone sidebar.
        self.assertIn(
            "#atlas-project-sidebar-list :is(.atlas-project-sidebar-row,.atlas-project-sidebar-all)",
            js,
        )

    def test_new_labels_exist_in_every_language(self) -> None:
        locale = read("project_locale.js")
        for key in ("projectActions", "rename"):
            match = re.search(rf"^\s+{key}: \[([^\]]*)\],$", locale, re.M)
            self.assertIsNotNone(match, key)
            self.assertEqual(
                len(re.findall(r"'[^']+'", match.group(1) if match else "")), 5
            )


class LongMessageTests(unittest.TestCase):
    def test_threshold_and_preview_are_one_screen_and_half_of_it(self) -> None:
        js = read("chat-ui.js")
        css = read("chat-ui.css")
        self.assertIn("const LONG_MESSAGE_LINES = 25;", js)
        self.assertIn("lineHeight(content) * (LONG_MESSAGE_LINES + 0.1)", js)
        self.assertNotIn("lineHeight(content) * 6.1", js)
        self.assertIn("max-height:calc(var(--atlas-body-size) * 1.5 * 12)", css)

    def test_long_message_has_no_accent_edge(self) -> None:
        css = read("chat-ui.css")
        rule = re.search(r"\.user-message\.atlas-long-message \{[^}]*\}", css)
        self.assertIsNotNone(rule)
        self.assertNotIn("border-left", rule.group(0) if rule else "")


class ImportedDrawingTests(unittest.TestCase):
    def test_project_answers_keep_app_links_and_generated_images(self) -> None:
        js = read("project_mode.js")
        self.assertNotIn(
            "renderer.image = () => ''", js[js.index("function prose") :][:4000]
        )
        self.assertIn(
            "const artifactRoute = /^\\/api\\/atlas\\/artifacts\\/([0-9a-f]{32})\\/versions\\/([1-9][0-9]{0,3})\\/(open|download)$/;",
            js,
        )
        self.assertIn(
            "const generatedImage = /^\\/images\\/(?:svg\\/)?[a-f0-9]{32}$/;", js
        )
        self.assertIn("window.atlasSideView.open(app.descriptor)", js)
        self.assertIn("openLatestApp();", js)
        self.assertIn("plain.append(answerLink(match[1],match[2]))", js)

    def test_images_outside_the_instance_routes_are_not_loaded(self) -> None:
        js = read("project_mode.js")
        body = js[js.index("function answerImage") :][:400]
        self.assertIn("!generatedImage.test(url.pathname)", body)
        self.assertIn("image.src = url.pathname", body)


if __name__ == "__main__":
    unittest.main()
