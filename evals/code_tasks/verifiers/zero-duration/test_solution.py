import unittest
from solution import duration


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(duration(0), "0m")


if __name__ == "__main__":
    unittest.main()
