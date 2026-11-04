"""OpenAI-format embeddings on the key API (phase 2 of API parity), without provider I/O."""

import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from services.orchestrator.api_keys import Store
from services.orchestrator.chat_api import app

URL = "/api/atlas/v1/embeddings"


class EmbeddingsTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(
            root / "keys.sqlite", "fictional-pepper-" * 4, lambda: 1800000000.0
        )
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ATLAS_DATA_DIR": str(root),
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_PROJECT_DSN": "",
                    "ATLAS_USER_DAILY_EUR": "10",
                    "ATLAS_GLOBAL_DAILY_EUR": "20",
                },
            )
        )
        self.enterContext(
            patch(
                "services.orchestrator.api_key_edge.configured_store",
                return_value=self.store,
            )
        )
        self.calls: list[dict[str, Any]] = []

        async def fake_embed(texts: list[str], **options: Any) -> dict[str, Any]:
            self.calls.append({"texts": texts, **options})
            size = options["dimensions"] or 4
            return {
                "vectors": [[float(i)] * size for i in range(len(texts))],
                "tokens": 7 * len(texts),
                "cost_micro": 1,
            }

        self.enterContext(patch("embeddings.embed", fake_embed))
        self.client = self.enterContext(TestClient(app))

    def key(self, scopes: list[str]) -> dict[str, str]:
        _, secret = self.store.create(
            org_id="fictional-org",
            member_id="fictional-member",
            subject="tester@example.invalid",
            budget_user="fictional-user",
            name="embeddings",
            scopes=scopes,
            created_by="operator",
            daily_requests=100,
            daily_micro_eur=1000000,
        )
        return {"Authorization": "Bearer " + secret}

    def test_vectors_in_input_order_with_usage(self) -> None:
        reply = self.client.post(
            URL,
            headers=self.key(["model"]),
            json={
                "model": "text-embedding-3-small",
                "input": ["un", "deux"],
                "dimensions": 64,
            },
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        body = reply.json()
        self.assertEqual(body["model"], "atlas-embed")
        self.assertEqual([row["index"] for row in body["data"]], [0, 1])
        self.assertEqual(len(body["data"][1]["embedding"]), 64)
        self.assertEqual(body["usage"], {"prompt_tokens": 14, "total_tokens": 14})
        self.assertEqual(self.calls[-1]["encoding"], "float")
        single = self.client.post(
            URL, headers=self.key(["model"]), json={"input": "seul"}
        )
        self.assertEqual(len(single.json()["data"]), 1)

    def test_refusals_name_the_problem(self) -> None:
        headers = self.key(["model"])
        for body, named in (
            ({"input": [1, 2, 3]}, "token arrays"),
            ({"input": ""}, "non-empty"),
            ({"input": "x", "dimensions": 8192}, "dimensions"),
            ({"input": "x", "encoding_format": "int8"}, "encoding_format"),
            ({"input": "x", "truncate": True}, "truncate"),
        ):
            reply = self.client.post(URL, headers=headers, json=body)
            self.assertEqual(reply.status_code, 400, reply.text)
            self.assertIn(named, reply.text)
        reply = self.client.post(URL, headers=headers, json={"input": ["a"] * 2049})
        self.assertEqual(reply.json()["error"]["type"], "input_limit")
        reply = self.client.post(
            URL, headers=headers, json={"input": ["x" * 100000] * 90}
        )
        self.assertEqual(reply.json()["error"]["type"], "input_limit")
        reply = self.client.post(
            URL,
            headers=headers | {"Content-Type": "application/json"},
            content=b'{"input": "' + b"x" * (17 * 1024 * 1024) + b'"}',
        )
        self.assertEqual(reply.status_code, 413, reply.text[:200])
        self.assertEqual(self.calls, [])

    def test_scope_strict_privacy_and_model_list(self) -> None:
        reply = self.client.post(
            URL, headers=self.key(["assistant"]), json={"input": "x"}
        )
        self.assertEqual(reply.status_code, 403, reply.text)
        reply = self.client.post(
            URL,
            headers=self.key(["model"]) | {"x-atlas-privacy": "strict"},
            json={"input": "x"},
        )
        self.assertEqual(reply.status_code, 503, reply.text)
        self.assertEqual(self.calls, [])
        names = [
            row["id"]
            for row in self.client.get(
                "/api/atlas/v1/models", headers=self.key(["model"])
            ).json()["data"]
        ]
        self.assertIn("atlas-embed", names)


if __name__ == "__main__":
    unittest.main()
