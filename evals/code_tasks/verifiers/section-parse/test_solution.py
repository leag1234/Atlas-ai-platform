import unittest
import solution


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(
            solution.parse("# note\n[server]\nport = 6100\n[worker]\ncount=2"),
            {"server": {"port": "6100"}, "worker": {"count": "2"}},
        )


if __name__ == "__main__":
    unittest.main()
