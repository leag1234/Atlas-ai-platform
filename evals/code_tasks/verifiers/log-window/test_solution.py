import unittest
import solution


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(
            solution.recent("one\r\ntwo\r\nthree\r\n", 2), ["two", "three"]
        )


if __name__ == "__main__":
    unittest.main()
