import unittest
import solution


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(
            list(
                solution.summarize(
                    [{"kind": "open"}, {"kind": "close"}, {"kind": "open"}]
                ).items()
            ),
            [("open", 2), ("close", 1)],
        )


if __name__ == "__main__":
    unittest.main()
