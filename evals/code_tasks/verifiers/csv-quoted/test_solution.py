import unittest
import solution


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(
            solution.fields('alpha, "two, words", omega'),
            ["alpha", "two, words", "omega"],
        )


if __name__ == "__main__":
    unittest.main()
