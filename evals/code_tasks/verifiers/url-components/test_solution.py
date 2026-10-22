import unittest
import solution


class BehaviorTests(unittest.TestCase):
    def test_expected_behavior(self):
        self.assertEqual(
            solution.build(
                "https://example.test/", ["/api/", "items"], {"q": "ok", "page": ""}
            ),
            "https://example.test/api/items?q=ok",
        )


if __name__ == "__main__":
    unittest.main()
