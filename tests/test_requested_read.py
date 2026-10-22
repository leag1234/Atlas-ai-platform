"""An explicit reading request executes an acquired URL before answering."""

import json
from decimal import Decimal
import unittest

from services.orchestrator.loop import (
    Call,
    Limits,
    Message,
    Query,
    Reservation,
    Turn,
    run,
)


class RequestedReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_then_read_share_tool_ledger(self) -> None:
        class Model:
            def estimate(self, messages: list[Message]) -> Reservation:
                return Reservation(1, Decimal(0))

            async def complete(self, messages: list[Message], timeout: float) -> Turn:
                return Turn(
                    "The source states the answer.", (), Reservation(1, Decimal(0))
                )

        class Tools:
            def __init__(self) -> None:
                self.names: list[str] = []

            def estimate(self, call: Call) -> Reservation:
                return Reservation(0, Decimal(0))

            async def execute(self, call: Call, timeout: float) -> Message:
                self.names.append(call.name)
                if call.name == "web_search":
                    return {"results": [{"link": "https://example.org/source"}]}
                return {"text": "The source states the answer."}

        tools = Tools()
        result = await run(
            Query(question="Search the web then read a source", lang="en"),
            Model(),
            tools,
            "",
            Limits(),
            initial_calls=(
                Call("search", "web_search", json.dumps({"query": "source"})),
            ),
        )
        self.assertEqual(tools.names, ["web_search", "web_fetch"])
        self.assertEqual(result.tool_calls, 2)
        self.assertIn("source states", result.text)
