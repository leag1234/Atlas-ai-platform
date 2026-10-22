"""HTTP auth, real SSE framing, quota reconciliation and cancellation without live I/O."""

import json
from time import monotonic
import unittest

import dev_gateway as gateway
from services.orchestrator.dev_chat import ChatInput, Output
from services.orchestrator.dev_inference import drive
from test_dev_chat import BODY
from devapi_support import Environment


class DevInferenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.env = Environment()
        self.addCleanup(self.env.close)

    async def test_http_completion_streaming_and_quota(self) -> None:
        response = self.env.client.post(
            "/v1/chat/completions", headers=self.env.headers, json=BODY
        )
        self.assertEqual(response.status_code, 200, response.text)
        choice = response.json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(
            json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"]),
            {"value": 7},
        )
        first = self.env.store.usage("alice")["charged_micro_eur"]
        response = self.env.client.post(
            "/v1/chat/completions",
            headers=self.env.headers,
            json=BODY | {"stream": True, "stream_options": {"include_usage": True}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.text.endswith("data: [DONE]\n\n"))
        frames = [
            json.loads(line[5:])
            for line in response.text.splitlines()
            if line.startswith("data:") and line[5:].strip() != "[DONE]"
        ]
        self.assertEqual(frames[0]["choices"][0]["delta"]["role"], "assistant")
        self.assertEqual(frames[-1]["usage"]["prompt_tokens"], 166)
        self.assertGreater(len(frames), 3)
        usage = self.env.store.usage("alice")
        self.assertEqual(usage["unknown_micro_eur"], 0)
        self.assertEqual(usage["charged_micro_eur"], int(str(first)) * 2)
        self.assertEqual(
            self.env.client.post(
                "/v1/chat/completions", headers=self.env.headers, json=BODY
            ).status_code,
            429,
        )
        self.assertEqual(len(self.env.inputs), 2)

    async def test_invalid_inputs_and_provider_failure(self) -> None:
        self.assertEqual(
            self.env.client.post("/v1/chat/completions", json=BODY).status_code, 401
        )
        for payload in (
            BODY | {"model": "outside"},
            BODY | {"messages": None},
            BODY | {"messages": [{"role": "user", "content": "x" * 50000}]},
        ):
            self.assertEqual(
                self.env.client.post(
                    "/v1/chat/completions", headers=self.env.headers, json=payload
                ).status_code,
                400,
            )
        self.assertEqual(self.env.inputs, [])
        response = self.env.client.post(
            "/v1/chat/completions", headers=self.env.headers, content=b"x" * 1048577
        )
        self.assertEqual(response.status_code, 400)
        self.env.raw = b"data: {}\n\n"
        self.assertEqual(
            self.env.client.post(
                "/v1/chat/completions", headers=self.env.headers, json=BODY
            ).status_code,
            502,
        )
        usage = self.env.store.usage("alice")
        self.assertGreater(int(str(usage["unknown_micro_eur"])), 0)
        self.assertEqual(usage["charged_micro_eur"], usage["unknown_micro_eur"])
        self.assertEqual(len(self.env.inputs), 1)

    async def test_generator_close_preserves_reserve_and_closes_transport(self) -> None:
        request = ChatInput.model_validate(BODY)
        plan = gateway.prepare(request)
        request_id = self.env.store.reserve("alice", plan.reserved)
        frames = drive(
            self.env.store,
            "alice",
            request_id,
            plan,
            Output(request_id, request),
            monotonic() + 120,
        )
        await frames.__anext__()
        await frames.aclose()
        self.assertTrue(self.env.closed)
        self.assertEqual(
            self.env.store.usage("alice")["unknown_micro_eur"], plan.reserved
        )

    async def test_expired_deadline_and_known_cost_above_reserve(self) -> None:
        from dataclasses import replace

        for expired in (True, False):
            request = ChatInput.model_validate(BODY)
            plan = gateway.prepare(request)
            if not expired:
                plan = replace(plan, reserved=1)
            request_id = self.env.store.reserve("alice", plan.reserved)
            frames = [
                f
                async for f in drive(
                    self.env.store,
                    "alice",
                    request_id,
                    plan,
                    Output(request_id, request),
                    monotonic() + (-1 if expired else 120),
                )
            ]
            self.assertIn("error", frames[-1])
            self.assertEqual(len(self.env.inputs), int(not expired))
        self.assertGreater(
            int(str(self.env.store.usage("alice")["charged_micro_eur"])), 1
        )
