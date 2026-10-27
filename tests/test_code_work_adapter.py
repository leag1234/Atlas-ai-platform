"""Measurement uses product request settings and stops before a fourth failure."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from evals.code_tasks.work_adapter import WorkAdapter
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.code_mode import configure_work_model, code_profile


class WorkAdapterTests(unittest.TestCase):
    def test_output_allowance_comes_from_validated_product_request(self) -> None:
        from agent_provider import AgentRequest

        request = ChatRequest.model_validate(
            dict(
                model=code_profile(),
                messages=[dict(role="user", content="Repair this independent fixture")],
            )
        )
        model = MagicMock()
        configure_work_model(model, request)
        profile, maximum = model.configure_quality.call_args.args
        self.assertEqual(maximum, request.max_tokens)
        AgentRequest.model_validate(
            dict(
                profile=profile,
                max_tokens=maximum,
                messages=[dict(role="user", content="Fixture")],
                tools=[],
                timeout=30.0,
            )
        )
        with self.assertRaises(ValueError):
            ChatRequest.model_validate(
                dict(
                    model=code_profile(),
                    max_tokens=8192,
                    messages=[dict(role="user", content="Fixture")],
                )
            )

    def test_provider_results_and_exceptions_count_together_and_prevent_dispatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = WorkAdapter("fixture", "fixture", Path(directory))
            adapter.record_outcome(dict(reason="provider_error"))
            adapter.record_outcome(dict(error="TimeoutError"))
            adapter.record_outcome(dict(reason="provider_error"))
            self.assertTrue(adapter.stopped)
            with patch("evals.code_tasks.work_adapter.public") as dispatch:
                with self.assertRaisesRegex(
                    RuntimeError, "three_consecutive_failures_stop"
                ):
                    adapter("Never dispatched", Path(directory))
                with self.assertRaisesRegex(
                    RuntimeError, "three_consecutive_failures_stop"
                ):
                    asyncio.run(adapter.run("Never dispatched", Path(directory)))
                dispatch.assert_not_called()

    def test_success_breaks_failure_streak_without_resetting_spend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = WorkAdapter("fixture", "fixture", Path(directory))
            adapter.record_outcome(dict(reason="provider_error"))
            adapter.record_outcome(dict(reason="provider_error"))
            adapter.record_outcome(dict(state="done", reason=None))
            adapter.record_outcome(dict(reason="provider_error"))
            self.assertFalse(adapter.stopped)
            self.assertEqual(adapter.consecutive_failures, 1)
