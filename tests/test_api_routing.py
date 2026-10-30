"""Public usage routing is configuration-driven and has no provider I/O."""

import os
import unittest
from unittest.mock import patch

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


class RoutedBodyTests(unittest.TestCase):
    def test_routed_body_has_no_empty_tool_fields(self) -> None:
        import json as _json
        import dev_gateway
        from packages.dev_request import Request

        request = Request.model_validate(
            {
                "messages": [{"role": "user", "content": "Reply OK"}],
                "max_tokens": 20,
                "reasoning_effort": "low",
            }
        )
        with patch.dict(
            os.environ,
            {"MISTRAL_API_KEY": "test-only", "SCW_GENERATIVE_API_KEY": "test-only"},
        ):
            plan = dev_gateway.prepare_api(request, "atlas-code", "key-test")
        body = _json.loads(plan.body)
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)
        self.assertNotIn("response_format", body)
        self.assertTrue(all("tool_calls" not in m for m in body["messages"]))
