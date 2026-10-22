import unittest
import solution


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(
            solution.order({"build": ["lint"], "lint": []}), ["lint", "build"]
        )


if __name__ == "__main__":
    unittest.main()
