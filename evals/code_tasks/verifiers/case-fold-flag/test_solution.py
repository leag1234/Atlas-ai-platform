import unittest
from solution import enabled


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(enabled(" YES "), True)


if __name__ == "__main__":
    unittest.main()
