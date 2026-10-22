import unittest
import solution


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(
            solution.parse_query("mode=&limit=10"), {"mode": "", "limit": "10"}
        )


if __name__ == "__main__":
    unittest.main()
