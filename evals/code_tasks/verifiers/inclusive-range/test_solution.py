import unittest
from solution import numbers


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(numbers(3, 5), [3, 4, 5])


if __name__ == "__main__":
    unittest.main()
