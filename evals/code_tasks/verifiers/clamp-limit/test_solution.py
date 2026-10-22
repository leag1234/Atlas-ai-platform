import unittest
from solution import clamp


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(clamp(12, 0, 10), 10)


if __name__ == "__main__":
    unittest.main()
