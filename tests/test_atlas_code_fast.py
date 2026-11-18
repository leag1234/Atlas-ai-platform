"""atlas-code-fast always serves the fast code chain, whatever effort the client sends."""

from collections.abc import AsyncGenerator
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from api_routing import EFFORTS, PUBLIC_MODELS, select_public_model, targets
import dev_gateway as gateway
from packages.dev_request import Request
from services.orchestrator.api_keys import Store
from services.orchestrator.chat_api import app

FAST = "zai-glm-5-3-flash"
FALLBACK = "deepseek-v4-flash-0731"


class FastRoutingTests(unittest.TestCase):
    def test_every_effort_resolves_to_the_none_chain(self) -> None:
        _, reference = targets("atlas-code", "none")
        self.assertEqual(reference[0].model, FAST)
        self.assertIn("atlas-code-fast", PUBLIC_MODELS)
        self.assertEqual(select_public_model("atlas-code-fast"), "atlas-code-fast")
        for effort in EFFORTS:
            usage, entries = targets("atlas-code-fast", effort)
            self.assertEqual(usage, "atlas-code-fast")
            self.assertEqual(entries, reference, effort)

    def test_plan_matches_atlas_code_at_effort_none(self) -> None:
        def plan(usage: str, effort: str) -> gateway.Plan:
            request = Request.model_validate(
                {
                    "messages": [{"role": "user", "content": "Reply OK"}],
                    "reasoning_effort": effort,
                }
            )
            return gateway.prepare_api(request, usage, "key-test")

        with patch.dict(
            os.environ,
            {"MISTRAL_API_KEY": "test-only", "SCW_GENERATIVE_API_KEY": "test-only"},
        ):
            reference = plan("atlas-code", "none")
            for effort in EFFORTS:
                fast = plan("atlas-code-fast", effort)
                self.assertEqual(fast.model, FAST)
                self.assertEqual(fast.body, reference.body, effort)
                assert fast.fallback is not None and reference.fallback is not None
                self.assertEqual(fast.fallback.model, FALLBACK)
                self.assertEqual(fast.fallback.body, reference.fallback.body, effort)


class FastRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = Store(root / "keys.sqlite", "fictional-pepper-" * 4, lambda: 1.8e9)
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ATLAS_DATA_DIR": str(root),
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_PROJECT_DSN": "",
                    "ATLAS_RAW_CALL_MAX_MICRO_EUR": "5000000",
                    "ATLAS_USER_DAILY_EUR": "10",
                    "ATLAS_GLOBAL_DAILY_EUR": "20",
                    "ATLAS_PROVIDER": "",
                    "ATLAS_API_DEFAULT_RAW_MODEL": "",
                    "SCW_GENERATIVE_BASE_URL": "https://provider.invalid/v1",
                },
            )
        )
        self.enterContext(
            patch(
                "services.orchestrator.api_key_edge.configured_store",
                return_value=store,
            )
        )
        self.plans: list[gateway.Plan] = []
        self.enterContext(patch.object(gateway, "transport", self.transport))
        self.client = self.enterContext(TestClient(app))
        _, self.secret = store.create(
            org_id="fictional-org",
            member_id="fictional-member",
            subject="tester@example.invalid",
            budget_user="fictional-user",
            name="coding",
            scopes=["model"],
            created_by="operator",
            daily_requests=20,
            daily_micro_eur=1000000,
        )

    async def transport(self, plan: gateway.Plan) -> AsyncGenerator[bytes, None]:
        self.plans.append(plan)
        for delta, reason in (({"content": "OK"}, None), ({}, "stop")):
            frame = {"choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
            yield ("data: " + json.dumps(frame) + "\n\n").encode()
        yield b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}\n\ndata: [DONE]\n\n'

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": "Bearer " + self.secret}
        if path == "/messages":
            headers = {"x-api-key": self.secret, "anthropic-version": "2023-06-01"}
        reply = self.client.post("/api/atlas/v1" + path, json=body, headers=headers)
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.headers["x-atlas-served-by"], "mistral/" + FAST)
        return dict(reply.json())

    def sent(self) -> tuple[gateway.Plan, dict[str, Any]]:
        plan = self.plans[-1]
        return plan, json.loads(plan.body)

    def test_high_effort_and_adaptive_thinking_stay_on_the_fast_chain(self) -> None:
        question = [{"role": "user", "content": "Reply OK"}]
        for path, body in (
            (
                "/chat/completions",
                {"messages": question, "reasoning_effort": "high"},
            ),
            (
                "/responses",
                {"input": "Reply OK", "reasoning": {"effort": "high"}, "store": False},
            ),
            (
                # What Claude Code sends on every request, thinking disabled or not.
                "/messages",
                {
                    "max_tokens": 64,
                    "messages": question,
                    "thinking": {"type": "adaptive"},
                    "output_config": {"effort": "high"},
                },
            ),
        ):
            with self.subTest(path=path):
                reply = self.post(path, body | {"model": "atlas-code-fast"})
                self.assertEqual(reply["model"], "atlas-code-fast")
                plan, sent = self.sent()
                self.assertEqual((plan.model, sent["model"]), (FAST, FAST))
                self.assertEqual(sent["reasoning_effort"], "low")
                assert plan.fallback is not None
                self.assertEqual(plan.fallback.model, FALLBACK)
                self.assertEqual(
                    json.loads(plan.fallback.body)["reasoning_effort"], "none"
                )

    def test_same_messages_request_on_atlas_dev_uses_the_dear_tier(self) -> None:
        self.client.post(
            "/api/atlas/v1/messages",
            json={
                "model": "atlas-dev",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "Reply OK"}],
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": "high"},
            },
            headers={"x-api-key": self.secret, "anthropic-version": "2023-06-01"},
        )
        self.assertNotEqual(self.plans[-1].model, FAST)

    def test_listed_by_models(self) -> None:
        headers = {"Authorization": "Bearer " + self.secret}
        listed = self.client.get("/api/atlas/v1/models", headers=headers).json()
        self.assertIn("atlas-code-fast", [entry["id"] for entry in listed["data"]])
        one = self.client.get("/api/atlas/v1/models/atlas-code-fast", headers=headers)
        self.assertEqual(one.status_code, 200, one.text)
        self.assertEqual(one.json()["owned_by"], "atlas")


if __name__ == "__main__":
    unittest.main()
