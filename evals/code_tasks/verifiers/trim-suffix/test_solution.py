import unittest
from solution import trim_suffix


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(trim_suffix("notes.csv", ".csv"), "notes")


if __name__ == "__main__":
    unittest.main()
