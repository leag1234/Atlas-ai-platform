"""Formatting retries stay in the image operation's original cost and time budget."""

from decimal import Decimal
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from threading import Thread
import unittest
from unittest.mock import AsyncMock, Mock, patch

import image_prompt
from agent_provider import AgentProvider
import imagegen
from gateway_cpu import CPUModels
from http_gateway import serve
from packages.configuration import MissingConfiguration
from packages.ports import atlas_ports
from services.orchestrator.model import GatewayError, GatewayModel
from serverless import ServerlessPolicy
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
        self.reserve = patch.object(
            image_prompt, "reservation", return_value=Decimal("0.01")
        )
        self.reserve.start()
        self.addCleanup(self.reserve.stop)

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
                    json.dumps(
                        {
                            "image": png(),
                            "seed": 7,
                            "steps": 4,
                            "model": "fixture-worker-model",
                        }
                    ).encode()
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

    async def test_preset_rewrite_uses_preset_endpoint_model_and_price(self) -> None:
        self.reserve.stop()
        environment = {
            "ATLAS_PROVIDER": "hetzner",
            "ATLAS_PROVIDER_API_KEY": "test-token",
        }
        request = {
            "messages": image_prompt.messages("a lighthouse"),
            "tools": [],
            "local_enabled": False,
            "timeout": 20,
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(
                AgentProvider,
                "_complete",
                AsyncMock(return_value={"text": FORMATTED, "usage": {}}),
            ) as complete,
        ):
            policy = ServerlessPolicy()
            self.assertEqual(
                image_prompt.reservation("a lighthouse"),
                policy.cost(
                    policy.models["vision"],
                    len(json.dumps(image_prompt.messages("a lighthouse")).encode())
                    + 256,
                    2048,
                ),
            )
            await image_prompt.ImageRewriteProvider().complete(request)
        assert complete.await_args is not None
        self.assertEqual(
            complete.await_args.args[1], "https://inference.hetzner.com/api/v1"
        )
        self.assertEqual(complete.await_args.args[2], policy.models["vision"])
        self.assertEqual(complete.await_args.args[3], "test-token")

    async def test_role_unavailable_uses_anchored_scene_without_a_provider_call(
        self,
    ) -> None:
        self.reserve.stop()
        prompt = "a young woman holding a copper fox beneath a cedar tree"
        budget = image_prompt.RewriteBudget(
            Decimal("0.05"), Decimal(0), structured=True
        )
        with (
            patch.dict(
                os.environ,
                {"ATLAS_PROVIDER": "mistral", "ATLAS_USD_TO_EUR": "0.9"},
                clear=True,
            ),
            patch.object(AgentProvider, "complete", AsyncMock()) as complete,
            self.assertLogs("image_prompt", level="INFO") as logs,
        ):
            self.assertEqual(
                image_prompt.reservation(prompt, structured=True), Decimal(0)
            )
            self.assertEqual(image_prompt.scene_reservation([prompt]), Decimal(0))
            result = await image_prompt.rewrite(prompt, 20, budget=budget)
            scene = await image_prompt.rewrite_scene([prompt], 20)
        complete.assert_not_awaited()
        self.assertEqual(result, prompt)
        self.assertIn(prompt, scene.render())
        event = json.loads(logs.output[-1].partition("image_prompt:")[2])
        self.assertEqual(event["event"], "image_rewrite_usage")
        self.assertEqual(event["fallback"], "role_unavailable")

    async def test_scaleway_rewrite_keeps_legacy_configuration(self) -> None:
        environment = {
            "SCW_GENERATIVE_BASE_URL": "https://legacy.example/v1",
            "SCW_GENERATIVE_API_KEY": "test-token",
            "ESCALATION_MODEL": "qwen3.5-397b-a17b",
        }
        request = {
            "messages": image_prompt.messages("a lighthouse"),
            "tools": [],
            "local_enabled": False,
            "timeout": 20,
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(
                AgentProvider,
                "_complete",
                AsyncMock(return_value={"text": FORMATTED, "usage": {}}),
            ) as complete,
        ):
            policy = ServerlessPolicy()
            await image_prompt.ImageRewriteProvider().complete(request)
        assert complete.await_args is not None
        self.assertEqual(
            complete.await_args.args[1], environment["SCW_GENERATIVE_BASE_URL"]
        )
        self.assertEqual(complete.await_args.args[2], policy.models["vision"])
        self.assertEqual(
            complete.await_args.args[3], environment["SCW_GENERATIVE_API_KEY"]
        )

    async def test_configuration_error_is_not_reported_as_provider_failure(
        self,
    ) -> None:
        self.reserve.stop()
        with patch.dict(os.environ, {"ATLAS_PROVIDER": "other"}, clear=True):
            with self.assertRaises(MissingConfiguration):
                image_prompt.rewrite_plan()
        server = serve(Mock(spec=CPUModels), 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with (
                patch.object(
                    imagegen,
                    "complete",
                    AsyncMock(
                        side_effect=MissingConfiguration(("ATLAS_PROVIDER_BASE_URL",))
                    ),
                ),
                self.assertRaises(GatewayError) as raised,
            ):
                await GatewayModel.post(
                    f"http://127.0.0.1:{server.server_port}/images/generate",
                    {"prompt": "a lighthouse"},
                    5,
                )
            self.assertEqual(raised.exception.code, "configuration_missing")
            self.assertEqual(raised.exception.status, 503)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
