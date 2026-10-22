"""Completed calls consume offered quotas without relaxing the harness limits."""

import unittest
from decimal import Decimal

from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.loop import Message


class AvailabilityTests(unittest.TestCase):
    def test_completed_calls_remove_only_exhausted_tools(self) -> None:
        model = GatewayModel(
            "http://localhost",
            Configuration(
                input_eur_per_mtok=Decimal(1),
                output_eur_per_mtok=Decimal(1),
                max_tokens=2048,
            ),
        )
        messages: list[Message] = []
        for index in range(3):
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": str(index), "function": {"name": "web_search"}}
                        ],
                    },
                    {"role": "tool", "tool_call_id": str(index), "content": "{}"},
                ]
            )

        def names(rows: list[dict[str, object]]) -> set[str]:
            return {
                str(f["name"]) for t in rows if isinstance(f := t.get("function"), dict)
            }

        self.assertIn("web_search", names(model.available_tools(messages[:-1])))
        search = next(
            row["function"]
            for row in model.available_tools(messages[:-1])
            if isinstance(row["function"], dict)
            and row["function"]["name"] == "web_search"
        )
        self.assertIn("Remaining calls in this request: 1", search["description"])
        self.assertIn("only one tool call per turn", search["description"])
        self.assertNotIn("Remaining calls", str(model.tools))
        self.assertNotIn("web_search", names(model.available_tools(messages)))
        self.assertIn("web_fetch", names(model.available_tools(messages)))
        self.assertIn("calculator", names(model.available_tools(messages)))
        for index in range(3, 10):
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": str(index), "function": {"name": "web_fetch"}}
                        ],
                    },
                    {"role": "tool", "tool_call_id": str(index), "content": "{}"},
                ]
            )
        self.assertEqual(model.available_tools(messages), [])
        messages = [
            {"role": "tool", "content": '{"error":"terminal_operation_failed"}'}
        ]
        self.assertEqual(model.available_tools(messages), [])
