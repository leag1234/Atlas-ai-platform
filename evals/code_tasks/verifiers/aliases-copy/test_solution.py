import unittest
import solution


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(
            (lambda result: (result["a"].append("x"), result["b"])[1])(
                solution.records(["a", "b"])
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
