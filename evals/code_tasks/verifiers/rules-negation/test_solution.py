import unittest
import solution


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(
            solution.matches("private/report", ["private", "public"], ["private/"]),
            False,
        )


if __name__ == "__main__":
    unittest.main()
