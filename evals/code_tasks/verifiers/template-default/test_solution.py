import unittest
import solution


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(
            solution.render("Hello {name}, {{ok}}", {}, {"name": "guest"}),
            "Hello guest, {ok}",
        )


if __name__ == "__main__":
    unittest.main()
