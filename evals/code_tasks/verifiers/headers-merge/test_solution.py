import unittest
import solution


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(
            solution.headers({"accept": "json", "trace": "on"}, {"accept": "text"}),
            {"accept": "text", "trace": "on"},
        )


if __name__ == "__main__":
    unittest.main()
