"""M8: task selection and reservation before any paid transport."""

import unittest
from decimal import Decimal
from unittest.mock import patch

from serverless import ServerlessPolicy, classify_task


class TaskRoutingTests(unittest.TestCase):
    def test_user_intent_and_image_priority(self) -> None:
        cases = [
            ("Bonjour, résume ce texte.", "text"),
            ("Écris une fonction Python qui trie les entiers.", "code"),
            ("Debug this SQL query: SELECT * FROM records", "code"),
            ("Refactor this JavaScript function", "code"),
            ("Write code to sort a list", "code"),
            ("Write a C++ function to sort a list", "code"),
            ("Écris une fonction qui trie une liste", "code"),
            ("Explique le code postal de Paris", "text"),
            ("🙂 Guten Tag", "text"),
        ]
        for content, expected in cases:
            with self.subTest(content=content):
                self.assertEqual(
                    classify_task([{"role": "user", "content": content}]), expected
                )
        self.assertEqual(
            classify_task(
                [
                    {"role": "system", "content": "You can write Python code."},
                    {"role": "user", "content": "Bonjour"},
                    {"role": "tool", "content": "SQL Python function"},
                ]
            ),
            "text",
        )
        self.assertEqual(
            classify_task(
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Write Python to describe this"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,test"},
                            },
                        ],
                    }
                ]
            ),
            "vision",
        )

    def test_empty_invalid_and_giant_messages(self) -> None:
        invalid: tuple[list[dict[str, object]], ...] = (
            [],
            [{"role": "user", "content": ""}],
            [{"role": "user", "content": 42}],
        )
        for messages in invalid:
            with self.assertRaises(ValueError):
                classify_task(messages)
        with self.assertRaisesRegex(ValueError, "context_exceeded"):
            ServerlessPolicy().reserve([{"role": "user", "content": "x" * 300000}], [])

    def test_reservation_covers_both_attempts(self) -> None:
        policy = ServerlessPolicy()
        messages: list[dict[str, object]] = [
            {"role": "user", "content": "Write a Python function"}
        ]
        plan = policy.reserve(messages, [])
        self.assertEqual(plan.task_type, "code")
        self.assertNotEqual(plan.primary, plan.fallback)
        self.assertGreater(plan.reserved_eur, Decimal("0"))
        self.assertLessEqual(plan.reserved_eur, Decimal("0.05"))
        self.assertGreaterEqual(
            plan.reserved_eur, plan.primary_bound + plan.fallback_bound
        )

    def test_missing_price_cannot_call_provider(self) -> None:
        with patch.dict("os.environ", {"CODE_MODEL": "unpriced-test-model"}):
            with self.assertRaisesRegex(ValueError, "missing_price"):
                ServerlessPolicy()


if __name__ == "__main__":
    unittest.main()


class PolicyBoundaryTests(unittest.TestCase):
    def test_cost_boundary_and_multibyte_counting(self) -> None:
        policy = ServerlessPolicy()
        with self.assertRaisesRegex(ValueError, "cost_budget"):
            policy.reserve([{"role": "user", "content": "a" * 30000}], [])
        with self.assertRaisesRegex(ValueError, "cost_budget"):
            policy.reserve([{"role": "user", "content": "🙂" * 7500}], [])
        plan = policy.reserve([{"role": "user", "content": "bonjour"}], [])
        self.assertGreater(plan.fallback_bound, plan.primary_bound)

    def test_unknown_capabilities_fail_closed(self) -> None:
        with patch.dict("os.environ", {"CODE_MODEL": "local"}):
            with self.assertRaisesRegex(ValueError, "missing_capabilities"):
                ServerlessPolicy()

    def test_configuration_covers_each_pair(self) -> None:
        policy = ServerlessPolicy()
        config = policy.configuration()
        for role in ("text", "code", "vision"):
            primary = policy.models[role]
            fallback = policy.models[policy.config[role]["fallback"]]
            bound = (
                1000 * Decimal(str(config["input_eur_per_mtok"]))
                + 2048 * Decimal(str(config["output_eur_per_mtok"]))
            ) / 1000000
            self.assertGreaterEqual(
                bound,
                policy.cost(primary, 1000, 2048) + policy.cost(fallback, 1000, 2048),
            )
