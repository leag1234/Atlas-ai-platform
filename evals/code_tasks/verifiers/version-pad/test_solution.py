import unittest
import solution


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(solution.newer("1.2.0", "1.2"), False)


if __name__ == "__main__":
    unittest.main()
