import unittest
import solution


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(
            solution.overlay(
                {"ui": {"theme": "light", "size": "m"}}, {"ui": {"theme": "dark"}}
            ),
            {"ui": {"theme": "dark", "size": "m"}},
        )


if __name__ == "__main__":
    unittest.main()
