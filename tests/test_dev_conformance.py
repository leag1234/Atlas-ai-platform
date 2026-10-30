"""REQ-DEV-003/004: exact OpenAI SDK wire shapes, without SDK dependencies."""

import json
import unittest
from typing import Any
from dataclasses import replace
from unittest.mock import patch

from devapi_support import Environment
from test_dev_chat import BODY
import dev_gateway as gateway


class DevConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Environment()
        self.addCleanup(self.env.close)

    def test_models_have_required_sdk_fields(self) -> None:
        response = self.env.client.get("/v1/models", headers=self.env.headers)
        self.assertEqual(response.status_code, 200)
        model = response.json()["data"][0]
        self.assertEqual(model["object"], "model")
        self.assertEqual(model["id"], BODY["model"])
        self.assertIsInstance(model["created"], int)
        self.assertEqual(model["owned_by"], "atlas")

    def test_explicit_single_choice_reaches_provider(self) -> None:
        response = self.env.client.post(
            "/v1/chat/completions", headers=self.env.headers, json=BODY | {"n": 1}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["choices"]), 1)
        self.assertNotIn("n", json.loads(self.env.inputs[0]))
        for value in (0, 2, True, "1", None):
            with self.subTest(n=value):
                invalid = self.env.client.post(
                    "/v1/chat/completions",
                    headers=self.env.headers,
                    json=BODY | {"n": value},
                )
                self.assertEqual(invalid.status_code, 400)
        self.assertEqual(len(self.env.inputs), 1)

    def test_usage_null_on_each_nonterminal_chat_chunk(self) -> None:
        prepare = gateway.prepare
        self.env.context.enter_context(
            patch.object(
                gateway,
                "prepare",
                lambda request: replace(
                    prepare(request),
                    provider="mistral",
                    model="fictional-model",
                ),
            )
        )
        for include in (True, False):
            response = self.env.client.post(
                "/v1/chat/completions",
                headers=self.env.headers,
                json=BODY
                | {"stream": True, "stream_options": {"include_usage": include}},
            )
            self.assertEqual(response.status_code, 200)
            lines = [
                line[6:]
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            self.assertEqual(lines[-1], "[DONE]")
            self.assertEqual(lines.count("[DONE]"), 1)
            chunks = [json.loads(line) for line in lines[:-1]]
            if include:
                self.assertEqual(chunks[-1]["choices"], [])
                self.assertGreater(chunks[-1]["usage"]["total_tokens"], 0)
                self.assertEqual(chunks[-1]["served_by"], "mistral/fictional-model")
                for chunk in chunks[:-1]:
                    self.assertIn("usage", chunk)
                    self.assertIsNone(chunk["usage"])
            else:
                self.assertEqual(chunks[-1]["served_by"], "mistral/fictional-model")
                for chunk in chunks:
                    self.assertNotIn("usage", chunk)

    def test_unsupported_routes_have_sdk_error_envelope(self) -> None:
        for endpoint in ("embeddings", "files", "images/generations"):
            response = self.env.client.post(
                "/v1/" + endpoint, headers=self.env.headers, json={}
            )
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.headers["content-type"], "application/json")
            self.assertEqual(response.json()["error"]["type"], "invalid_request_error")
            self.assertEqual(response.json()["error"]["message"], "Not Found")
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.env.inputs, [])

    def test_missing_response_fields_are_client_errors(self) -> None:
        payloads: list[dict[str, Any]] = [
            {"model": "atlas-code"},
            {"model": "atlas-code", "input": [{"role": "user"}]},
            {"input": "hello"},
            {"model": "atlas-code", "input": "hello", "tools": [{}]},
        ]
        for payload in payloads:
            response = self.env.client.post(
                "/v1/responses", headers=self.env.headers, json=payload
            )
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()["error"]["type"], "invalid_request")
        self.assertEqual(self.env.inputs, [])

    def test_error_after_first_chunk_preserves_sse_shape(self) -> None:
        first = self.env.raw.split(b"\n\n")[0] + b"\n\n"
        self.env.raw = first + b"data: {}\n\n"
        response = self.env.client.post(
            "/v1/chat/completions",
            headers=self.env.headers,
            json=BODY | {"stream": True, "stream_options": {"include_usage": True}},
        )
        self.assertEqual(response.status_code, 200)
        data = [
            line[6:] for line in response.text.splitlines() if line.startswith("data: ")
        ]
        self.assertEqual(
            json.loads(data[0])["choices"][0]["delta"]["role"], "assistant"
        )
        self.assertEqual(json.loads(data[-2])["error"]["type"], "provider_error")
        self.assertEqual(data[-1], "[DONE]")
        self.assertGreater(
            int(str(self.env.store.usage("alice")["unknown_micro_eur"])), 0
        )
