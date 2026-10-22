import unittest
from solution import allowed


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(allowed("reader,writer", "read"), False)


if __name__ == "__main__":
    unittest.main()
