import unittest
from solution import page


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(page(["a", "b", "c", "d"], 1, 2), ["c", "d"])


if __name__ == "__main__":
    unittest.main()
