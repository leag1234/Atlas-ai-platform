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


if __name__ == "__main__":
    unittest.main()
