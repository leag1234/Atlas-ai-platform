import unittest
import solution


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(
            solution.table([["name", "state"], ["a", "ready"], ["longer", "ok"]]),
            "name   | state\na      | ready\nlonger | ok   ",
        )


if __name__ == "__main__":
    unittest.main()
