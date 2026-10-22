"""The complete 24 KiB module reaches the gateway; budget overflow never does."""

import hashlib
import json
from pathlib import Path
import unittest
from devapi_context import context_case, context_answer
from devapi_support import Environment


class DevContextTests(unittest.TestCase):
    def test_full_module_and_recorded_answer(self) -> None:
        env = Environment()
        self.addCleanup(env.close)
        payload, expected, size = context_case()
        fixture = json.loads(Path("tests/fixtures/devapi-context.json").read_text())
        self.assertEqual(
            hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
            fixture["request_sha256"],
        )
        self.assertGreaterEqual(size, 24576)
        self.assertEqual(size, fixture["code_bytes"])
        env.raw = fixture["sse"].encode()
        reply = env.client.post(
            "/v1/chat/completions", headers=env.headers, json=payload
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(
            context_answer(reply.json()["choices"][0]["message"]["content"]), expected
        )
        upstream = json.loads(env.inputs[0])
        self.assertEqual(
            upstream["messages"][0],
            {
                "role": "system",
                "content": Path("prompts/code-language.txt").read_text(),
            },
        )
        self.assertEqual(upstream["messages"][1:], payload["messages"])
        self.assertEqual(
            hashlib.sha256(env.inputs[0].encode()).hexdigest(),
            fixture["gateway_request_sha256"],
        )
        usage = env.store.usage("alice")
        self.assertEqual(usage["requests"], 1)
        charged = usage["charged_micro_eur"]
        assert isinstance(charged, int)
        self.assertGreater(charged, 0)
        self.assertLessEqual(charged, 50000)
        self.assertEqual(usage["unknown_micro_eur"], 0)
        payload["messages"][-1]["content"] *= 2
        refused = env.client.post(
            "/v1/chat/completions", headers=env.headers, json=payload
        )
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()["error"]["type"], "cost_budget")
        self.assertEqual(len(env.inputs), 1)
        self.assertEqual(env.store.usage("alice"), usage)
