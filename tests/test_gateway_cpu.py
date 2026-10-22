import io
import json
import math
import unittest
from http.client import HTTPConnection
from threading import Thread
from http_gateway import serve
from contextlib import redirect_stdout
from typing import ClassVar
from unittest.mock import patch

import numpy as np

from gateway_cpu import CPUModels, main, validate_texts


class CPUTests(unittest.TestCase):
    backend: ClassVar[CPUModels]

    @classmethod
    def setUpClass(cls) -> None:
        cls.backend = CPUModels()

    def test_real_multilingual_embeddings_and_reranker(self) -> None:
        texts = [
            "La capitale de la France est Paris.",
            "Paris is the capital of France.",
            "Un chat dort sur le canapé.",
        ]
        vectors = self.backend.embed(texts)
        self.assertEqual(len(vectors), 3)
        self.assertTrue(all(len(v) == 384 for v in vectors))
        self.assertTrue(
            all(math.isclose(sum(x * x for x in v), 1.0, abs_tol=1e-5) for v in vectors)
        )
        similarity = [
            sum(a * b for a, b in zip(vectors[0], row)) for row in vectors[1:]
        ]
        self.assertGreater(similarity[0], similarity[1])
        scores = self.backend.rerank(
            "What is the capital of France?", [texts[2], texts[0]]
        )
        self.assertEqual(len(scores), 2)
        self.assertGreater(scores[1], scores[0])
        np.testing.assert_allclose(vectors, self.backend.embed(texts), atol=1e-6)

    def test_input_limits(self) -> None:
        for texts in [[], [" "], ["x"] * 33, ["x" * 32001]]:
            with self.subTest(texts=texts), self.assertRaises(ValueError):
                self.backend.embed(texts)
        with self.assertRaisesRegex(ValueError, "context_exceeded"):
            validate_texts(["x" * 32000] * 5, 32)
        with self.assertRaises(ValueError):
            self.backend.rerank("", ["text"])
        with self.assertRaises(ValueError):
            self.backend.rerank("question", ["x"] * 65)
        with self.assertRaisesRegex(ValueError, "context_exceeded"):
            self.backend.rerank("question", ["x" * 32000] * 4)

    def test_provider_outputs_validated(self) -> None:
        for output in [
            np.zeros((1, 384)),
            np.full((1, 384), np.nan),
            np.ones((2, 384)),
        ]:
            with (
                patch.object(self.backend.encoder, "encode", return_value=output),
                self.assertRaises(ValueError),
            ):
                self.backend.embed(["text"])
        for score_output in [[float("inf")], [1.0, 2.0]]:
            with (
                patch.object(self.backend.ranker, "predict", return_value=score_output),
                self.assertRaises(ValueError),
            ):
                self.backend.rerank("question", ["text"])

    def test_cli_uses_real_engines(self) -> None:
        for operation in ["embeddings", "rerank"]:
            request = {
                "operation": operation,
                "texts": ["Paris est en France."],
                "question": "Où est Paris ?",
            }
            output = io.StringIO()
            with (
                patch("sys.stdin", io.StringIO(json.dumps(request))),
                patch("gateway_cpu.CPUModels", return_value=self.backend),
                redirect_stdout(output),
            ):
                main()
            self.assertIn(
                "vectors" if operation == "embeddings" else "scores",
                json.loads(output.getvalue()),
            )
        for invalid_request in [
            {"operation": "bad", "texts": ["x"]},
            {"operation": "embeddings", "texts": [1]},
            {"operation": "rerank", "texts": ["x"], "question": 3},
        ]:
            with (
                patch("sys.stdin", io.StringIO(json.dumps(invalid_request))),
                patch("gateway_cpu.CPUModels", return_value=self.backend),
                self.assertRaises(ValueError),
            ):
                main()

    def test_http_contract(self) -> None:
        server = serve(self.backend, 0)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            for path, payload, expected in [
                ("/embeddings", {"texts": ["Bonjour"], "kind": "query"}, 200),
                (
                    "/rerank",
                    {
                        "question": "Paris ?",
                        "passages": [
                            {"chunk_id": "a" * 64, "text": "Paris est en France."}
                        ],
                    },
                    200,
                ),
                ("/embeddings", {"texts": [], "kind": "query"}, 400),
                ("/embeddings", {"texts": ["x" * 32000] * 5, "kind": "query"}, 413),
                (
                    "/rerank",
                    {"question": "x", "passages": [{"chunk_id": "bad", "text": "x"}]},
                    400,
                ),
                ("/missing", {}, 400),
            ]:
                client = HTTPConnection("127.0.0.1", server.server_port, timeout=30)
                try:
                    client.request("POST", path, json.dumps(payload))
                    result = client.getresponse()
                    self.assertEqual(result.status, expected)
                    self.assertIsInstance(json.loads(result.read()), dict)
                finally:
                    client.close()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
