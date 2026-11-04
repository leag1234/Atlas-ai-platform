"""Which pages the Messages web_fetch tool may read (no network)."""

import unittest

from services.orchestrator.hosted_tools import fetch_refusal

SEEN = "Read https://docs.example.invalid/a and https://other.invalid/b please"


class FetchRefusalTests(unittest.TestCase):
    def test_only_addresses_the_conversation_holds(self) -> None:
        self.assertIsNone(fetch_refusal("https://docs.example.invalid/a", SEEN, {}, 0))
        self.assertEqual(
            fetch_refusal("https://docs.example.invalid/secret", SEEN, {}, 0),
            "url_not_allowed",
        )
        self.assertEqual(
            fetch_refusal("file:///etc/passwd", SEEN, {}, 0), "invalid_input"
        )

    def test_domain_filters_and_use_limit(self) -> None:
        allowed = {"allowed_domains": ["example.invalid"]}
        self.assertIsNone(
            fetch_refusal("https://docs.example.invalid/a", SEEN, allowed, 0)
        )
        self.assertEqual(
            fetch_refusal("https://other.invalid/b", SEEN, allowed, 0),
            "url_not_allowed",
        )
        blocked = {"blocked_domains": ["other.invalid"]}
        self.assertEqual(
            fetch_refusal("https://other.invalid/b", SEEN, blocked, 0),
            "url_not_allowed",
        )
        self.assertEqual(
            fetch_refusal("https://other.invalid/b", SEEN, {"max_uses": 1}, 1),
            "max_uses_exceeded",
        )


if __name__ == "__main__":
    unittest.main()
