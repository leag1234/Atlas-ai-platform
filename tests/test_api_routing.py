"""Public usage routing is configuration-driven and has no provider I/O."""

import unittest

from api_routing import select_public_model, targets


class ApiRoutingTests(unittest.TestCase):
    def test_usage_effort_routes_and_aliases(self) -> None:
        for usage, effort, model in (
            ("atlas-code", "none", "zai-glm-5-3-flash"),
            ("atlas-code", "high", "zai-glm-5-3"),
            ("atlas-chat", "medium", "zai-glm-5-3"),
            ("atlas-chat-2026-10-30", "low", "zai-glm-5-3"),
        ):
            resolved, entries = targets(usage, effort)
            self.assertIn(resolved, ("atlas-code", "atlas-chat"))
            self.assertEqual(entries[0].model, model)
            self.assertEqual(entries[1].provider, "scaleway")

    def test_unknown_client_defaults_to_code_but_raw_is_refused(self) -> None:
        self.assertEqual(select_public_model("claude-fictional"), "atlas-code")
        self.assertEqual(select_public_model("gpt-fictional"), "atlas-code")
        with self.assertRaises(ValueError):
            select_public_model("raw/internal")
