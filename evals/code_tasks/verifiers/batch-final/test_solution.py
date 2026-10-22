import unittest
import solution


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(solution.batches([1, 2, 3], 2), [[1, 2], [3]])


if __name__ == "__main__":
    unittest.main()
