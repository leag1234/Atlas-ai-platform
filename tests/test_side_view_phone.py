"""On a phone the side view never opens by itself: it covered the whole conversation."""

from pathlib import Path
import unittest


class PhoneSideViewTests(unittest.TestCase):
    def test_automatic_opening_is_limited_to_wide_screens(self) -> None:
        script = Path("services/orchestrator/chat-ui.js").read_text()
        self.assertIn(
            'const wide = () => matchMedia("(min-width: 768px)").matches;', script
        )
        self.assertEqual(script.count("wide() && !opened.has(identity)"), 2)
        self.assertIn("wide() && (message === last", script)


class DialogDismissTests(unittest.TestCase):
    def test_atlas_dialogs_close_on_escape_and_backdrop(self) -> None:
        # On a phone the move dialog could only be closed with its Cancel button.
        script = Path("services/orchestrator/project_mode.js").read_text()
        self.assertIn("dialog.atlas-project-dialog[open]", script)
        self.assertIn("event.key !== 'Escape'", script)
        self.assertIn("dialog === backdropDown", script)


class PhoneNavigationTests(unittest.TestCase):
    def test_atlas_entries_close_the_phone_sidebar_and_project_header_opens_it(
        self,
    ) -> None:
        script = Path("services/orchestrator/project_mode.js").read_text()
        self.assertIn("const closePhoneSidebar = () =>", script)
        self.assertIn("#atlas-project-sidebar-list button", script)
        self.assertIn("atlas-project-sidebar-open", script)
        css = Path("services/orchestrator/project_mode.css").read_text()
        self.assertIn(".atlas-project-page-heading > button { white-space:nowrap;", css)


if __name__ == "__main__":
    unittest.main()
