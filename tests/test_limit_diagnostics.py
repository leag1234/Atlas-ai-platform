"""Numerical refusals retain measurements at public/tool boundaries."""

import unittest
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from services.orchestrator.calculator import calculate
from services.orchestrator.web import Web
from packages.limits import LimitError
from eval_provider import EvalProvider
from generation import AnswerRequest, parse_answer


class LimitDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_dispatch_separates_money_and_time_limits(self) -> None:
        from decimal import Decimal
        from services.orchestrator.imagegen import process_image
        from services.orchestrator.interactions import Interaction

        for budget, timeout, measured, threshold, unit in (
            (Decimal("0.06"), 118, 60000, 50000, "microEUR"),
            (Decimal("0.05"), 121, 121000, 120000, "milliseconds"),
        ):
            with self.subTest(unit=unit), self.assertRaises(LimitError) as caught:
                await process_image(
                    "a cube",
                    Interaction(question="a cube"),
                    language="en",
                    budget=budget,
                    timeout=timeout,
                )
            self.assertEqual(caught.exception.measured, measured)
            self.assertEqual(caught.exception.limit, threshold)
            self.assertEqual(caught.exception.unit, unit)

    def test_profile_money_limit_reports_requested_and_allowed_microeur(self) -> None:
        from agent_provider import AgentRequest
        from pydantic import ValidationError
        from packages.validation import describe_validation

        with self.assertRaises(ValidationError) as caught:
            AgentRequest(
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
                profile="atlas-qwen",
                timeout=120.0,
                budget_eur="0.11",
            )
        detail = describe_validation(caught.exception)
        self.assertIn("110000", detail)
        self.assertIn("100000", detail)

    async def test_image_reservation_reports_money_before_paid_io(self) -> None:
        import os
        from decimal import Decimal
        import imagegen

        with (
            patch.dict(
                os.environ,
                {"ATLAS_IMAGE_GPU_IP": "127.0.0.1", "ATLAS_IMAGE_GPU_EUR_H": "1.5"},
            ),
            patch.object(imagegen, "reservation", return_value=Decimal("0.06")),
            patch.object(imagegen, "rewrite", new_callable=AsyncMock) as rewrite,
            self.assertRaises(LimitError) as caught,
        ):
            await imagegen.generate({"prompt": "a cube"})
        self.assertEqual(caught.exception.measured, 60000)
        self.assertEqual(caught.exception.limit, 50000)
        rewrite.assert_not_called()

    def test_agent_deadline_limit_reports_milliseconds(self) -> None:
        from agent_provider import AgentRequest
        from pydantic import ValidationError
        from packages.validation import describe_validation

        with self.assertRaises(ValidationError) as caught:
            AgentRequest(
                messages=[{"role": "user", "content": "hello"}], tools=[], timeout=121.0
            )
        detail = describe_validation(caught.exception)
        self.assertIn("121000", detail)
        self.assertIn("120000", detail)

    def test_retrieval_cli_reports_oversized_envelope_before_parsing(self) -> None:
        from services.retrieval.tool import main

        with (
            patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(b"x" * 20000))),
            self.assertRaises(LimitError) as caught,
        ):
            main()
        self.assertEqual(caught.exception.measured, 16001)
        self.assertEqual(caught.exception.limit, 16000)

    def test_rag_answer_size_retains_numeric_diagnostic(self) -> None:
        request = AnswerRequest(question="Explain", passages=[])
        with self.assertRaises(LimitError) as caught:
            parse_answer("x" * 32001, request)
        self.assertEqual(caught.exception.measured, 32001)
        self.assertEqual(caught.exception.limit, 32000)

    def test_eval_input_limits_report_actual_measurements_before_io(self) -> None:
        provider = EvalProvider()
        for messages, threshold in [
            ([{"role": "user", "content": "hello"}] * 101, 100),
            ([{"role": "user", "content": "x" * 32001}], 32000),
        ]:
            with self.assertRaises(LimitError) as caught:
                provider.complete("system", messages)
            self.assertEqual(caught.exception.limit, threshold)
            self.assertGreater(caught.exception.measured, threshold)

    def test_calculator_reports_size_and_ast_limit(self) -> None:
        for expression, measured, limit in [
            ("1" * 513, 513, 512),
            ("1+" * 70 + "1", 212, 128),
        ]:
            with self.assertRaises(LimitError) as caught:
                calculate(expression)
            self.assertEqual(caught.exception.measured, measured)
            self.assertEqual(caught.exception.limit, limit)
            self.assertIn(str(measured), caught.exception.detail)
            self.assertIn(str(limit), caught.exception.detail)

    async def test_redirects_report_actual_attempt_count(self) -> None:
        web = Web()
        with patch.object(
            web, "_request", AsyncMock(return_value=(302, "/again", b""))
        ):
            with self.assertRaises(LimitError) as caught:
                await web._robots("https://example.com", 1)
        self.assertEqual(caught.exception.measured, 5)
        self.assertEqual(caught.exception.limit, 4)
