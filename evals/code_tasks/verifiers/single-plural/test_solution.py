import unittest
from solution import label


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(label(1), "1 file")


if __name__ == "__main__":
    unittest.main()
