"""Iteration preserves all user constraints before the image rewriting stage."""

from decimal import Decimal
from time import monotonic
import unittest
from unittest.mock import AsyncMock, patch
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.image_tool import finish
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Result


class ImageFidelityTests(unittest.IsolatedAsyncioTestCase):
    async def test_iteration_keeps_original_constraints_and_shared_file_budget(
        self,
    ) -> None:
        request = ChatRequest.model_validate(
            {
                "messages": [
                    {"role": "user", "content": "Draw a tree beside a red house."},
                    {"role": "assistant", "content": "![image](/images/example)"},
                    {"role": "user", "content": "Add a blue bird."},
                ]
            }
        )
        result = Result(cost=Decimal("0.08"))
        with patch(
            "services.orchestrator.image_tool.process_image", new_callable=AsyncMock
        ) as generate:
            await finish(request, Interaction(), result, "A blue bird", monotonic())
        self.assertEqual(
            generate.call_args.args[0],
            "Draw a tree beside a red house. Add a blue bird.",
        )
        self.assertEqual(generate.call_args.kwargs["budget"], Decimal("0.05"))
