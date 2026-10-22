import unittest
from solution import display_name


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(display_name(""), "untitled")


if __name__ == "__main__":
    unittest.main()
