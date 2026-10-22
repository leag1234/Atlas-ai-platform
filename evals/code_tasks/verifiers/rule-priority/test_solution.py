import unittest
import solution


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(
            solution.choose(
                [
                    {"name": "standard", "minimum": 0},
                    {"name": "priority", "minimum": 10},
                ],
                12,
            ),
            "priority",
        )


if __name__ == "__main__":
    unittest.main()
