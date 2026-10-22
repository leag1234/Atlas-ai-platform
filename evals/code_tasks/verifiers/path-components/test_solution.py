import unittest
import solution


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(solution.safe_path("exports/../secret.txt"), False)


if __name__ == "__main__":
    unittest.main()
