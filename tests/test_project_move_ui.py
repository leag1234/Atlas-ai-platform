"""Static checks: the move-menu dialog and the import copy stay complete."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ORCHESTRATOR = Path("services/orchestrator")


class ProjectMoveUiTests(unittest.TestCase):
    def test_dialog_carries_its_own_theme_variables(self) -> None:
        css = (ORCHESTRATOR / "project_mode.css").read_text()
        match = re.search(r"\.atlas-project-dialog\s*\{([^}]*)\}", css)
        self.assertIsNotNone(match)
        declarations = match.group(1) if match else ""
        for variable in (
            "--project-bg",
            "--project-card",
            "--project-line",
            "--project-ink",
            "--project-muted",
            "--project-accent",
            "--project-hover",
        ):
            self.assertIn(variable + ":", declarations)
        self.assertIn("background:var(--project-bg)", declarations)
        self.assertIn("color:var(--project-ink)", declarations)
        self.assertIn(".atlas-project-dialog::backdrop", css)

    def test_both_entry_points_open_the_same_dialog_class(self) -> None:
        js = (ORCHESTRATOR / "project_mode.js").read_text()
        self.assertIn("node('dialog', null, 'atlas-project-dialog')", js)
        self.assertIn("'atlas-project-dialog atlas-project-move-dialog'", js)
        self.assertNotIn("atlas-project-dialog-move", js)

    def test_move_copies_artifact_links_and_files_as_parts(self) -> None:
        js = (ORCHESTRATOR / "project_mode.js").read_text()
        self.assertIn("nativeParts", js)
        self.assertIn("artifact_link", js)
        self.assertIn("parts:", js)


if __name__ == "__main__":
    unittest.main()


class DarkMoveDialogTests(unittest.TestCase):
    def test_dark_move_rows_are_not_filled_buttons(self) -> None:
        css = Path("services/orchestrator/project_mode.css").read_text()
        self.assertIn(
            "html.dark .atlas-project-move-dialog form button, html.dark .atlas-project-move-menu button { background:transparent;",
            css,
        )
        self.assertIn(
            "#atlas-project .atlas-project-composer textarea { resize:none; }", css
        )
