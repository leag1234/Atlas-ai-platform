"""Formatting retries stay in the image operation's original cost and time budget."""

from decimal import Decimal
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from threading import Thread
import unittest
from unittest.mock import AsyncMock, patch

import image_prompt
from agent_provider import AgentProvider
import imagegen
from packages.ports import atlas_ports
from test_imagegen import png


FORMATTED = "Subjects: nude figure\nAttributes: charcoal lines\nScene: studio\nStyle: figure study"


class ImageRewriteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # CI runs without the owner environment: the provider reads these at construction.
        patcher = patch.dict(
            os.environ,
            {"ESCALATION_MODEL": "local", "SCW_GENERATIVE_API_KEY": "test-key"},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # The reservation looks up the serverless catalogue, which needs the owner environment.
        reserve = patch.object(
            image_prompt, "reservation", return_value=Decimal("0.01")
        )
        reserve.start()
        self.addCleanup(reserve.stop)

    async def test_valid_first_rewrite_has_no_retry(self) -> None:
        budget = image_prompt.RewriteBudget(Decimal("0.05"), Decimal("0.01"))
        with patch.object(
            AgentProvider,
            "complete",
            AsyncMock(return_value={"text": FORMATTED}),
        ) as complete:
            result = await image_prompt.rewrite(
                "nude figure study in charcoal", 30, "en", budget=budget
            )
        self.assertEqual(result, FORMATTED)
        complete.assert_awaited_once()
        self.assertEqual(budget.reserved, Decimal("0.01"))

    async def test_retry_formats_only_and_reserves_before_call(self) -> None:
        budget = image_prompt.RewriteBudget(Decimal("0.05"), Decimal("0.01"))

        async def complete(request: dict[str, object]) -> dict[str, object]:
            if budget.reserved == Decimal("0.01"):
                return {"text": "I cannot format that request."}
            self.assertEqual(budget.reserved, Decimal("0.02"))
            self.assertIn("formatting-only", str(request["messages"]))
            self.assertLess(float(str(request["timeout"])), 30)
            return {"text": FORMATTED}

        with (
            patch.object(
                AgentProvider, "complete", AsyncMock(side_effect=complete)
            ) as provider,
            patch.object(image_prompt, "reservation", return_value=Decimal("0.01")),
        ):
            result = await image_prompt.rewrite(
                "nude figure study in charcoal", 30, "en", budget=budget
            )
        self.assertEqual(provider.await_count, 2)
        self.assertEqual(result, FORMATTED)

    async def test_bad_outputs_fall_back_to_original_without_new_cost_ceiling(
        self,
    ) -> None:
        original = "génère une image de la Nativité"
        for invalid in (
            None,
            "I cannot do that.",
            "Subjects: Attributes: Scene: Style:",
            "x" * 2001,
        ):
            budget = image_prompt.RewriteBudget(Decimal("0.05"), Decimal("0.01"))
            with (
                self.subTest(invalid=invalid),
                patch.object(
                    AgentProvider,
                    "complete",
                    AsyncMock(return_value={"text": invalid}),
                ) as provider,
                patch.object(image_prompt, "reservation", return_value=Decimal("0.01")),
            ):
                self.assertEqual(
                    await image_prompt.rewrite(original, 30, "fr", budget=budget),
                    original,
                )
                self.assertEqual(provider.await_count, 2)
                self.assertEqual(budget.reserved, Decimal("0.02"))

    async def test_retry_is_skipped_if_budget_or_deadline_cannot_fit(self) -> None:
        for ceiling in ("0.02", "0.019"):
            budget = image_prompt.RewriteBudget(Decimal(ceiling), Decimal("0.01"))
            with (
                patch.object(
                    AgentProvider,
                    "complete",
                    AsyncMock(return_value={"text": "No."}),
                ) as provider,
                patch.object(image_prompt, "reservation", return_value=Decimal("0.01")),
            ):
                self.assertEqual(
                    await image_prompt.rewrite("a garden", 30, budget=budget),
                    "a garden",
                )
                provider.assert_awaited_once()
                self.assertEqual(budget.reserved, Decimal("0.01"))
        with patch.object(AgentProvider, "complete", AsyncMock()) as provider:
            self.assertEqual(await image_prompt.rewrite("a garden", 0), "a garden")
            provider.assert_not_awaited()

    async def test_provider_failures_are_not_disguised_as_success(self) -> None:
        with patch.object(
            AgentProvider,
            "complete",
            AsyncMock(side_effect=RuntimeError("quota exhausted")),
        ):
            with self.assertRaisesRegex(RuntimeError, "quota exhausted"):
                await image_prompt.rewrite("a garden", 30)

    async def test_refusal_fallback_reaches_worker_with_cost_accounted(self) -> None:
        requests: list[dict[str, object]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                requests.append(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
                self.send_response(200)
                self.end_headers()
                self.wfile.write(
                    json.dumps({"image": png(), "seed": 7, "steps": 4}).encode()
                )

        with ThreadingHTTPServer(
            ("127.0.0.2", atlas_ports().port("terminal")), Handler
        ) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with (
                    patch.dict(
                        "os.environ",
                        {
                            "ATLAS_IMAGE_GPU_IP": "127.0.0.2",
                            "ATLAS_IMAGE_GPU_EUR_H": "0.7875",
                        },
                    ),
                    patch.object(imagegen, "reservation", return_value=Decimal("0.01")),
                    patch.object(
                        image_prompt, "reservation", return_value=Decimal("0.01")
                    ),
                    patch.object(
                        AgentProvider,
                        "complete",
                        AsyncMock(return_value={"text": "I cannot do that."}),
                    ) as provider,
                ):
                    result = await imagegen.generate(
                        {
                            "prompt": "nude figure study in charcoal",
                            "max_cost_eur": "0.05",
                        }
                    )
                self.assertEqual(provider.await_count, 2)
                self.assertEqual(
                    requests,
                    [{"prompt": "nude figure study in charcoal", "seed": None}],
                )
                self.assertEqual(result["image"], png())
                self.assertGreater(float(str(result["cost_eur"])), 0.02)
                self.assertLess(float(str(result["cost_eur"])), 0.05)
            finally:
                server.shutdown()
                thread.join()
