import unittest
import solution


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(
            solution.slugs(["Release Notes", "Release Notes", "Guide"]),
            ["release-notes", "release-notes-2", "guide"],
        )


if __name__ == "__main__":
    unittest.main()
