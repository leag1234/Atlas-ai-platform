import unittest
from solution import unique


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(unique(["b", "a", "b"]), ["b", "a"])


if __name__ == "__main__":
    unittest.main()
