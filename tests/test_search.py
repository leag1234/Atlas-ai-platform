import unittest
from unittest.mock import patch

from packages.ports import atlas_ports
from services.retrieval.search import Gateway, rank, tokens
from services.retrieval.store import Chunk


class SearchTests(unittest.TestCase):
    def test_multilingual_tokens(self) -> None:
        self.assertEqual(tokens("ÉTÉ été"), ["été", "été"])
        self.assertIn("预", tokens("预算"))

    def test_hybrid_reranking_and_boundaries(self) -> None:
        chunks = [
            Chunk(
                chunk_id=c * 64,
                doc_id=c,
                source=c + ".md",
                langue="en",
                source_sha256="d" * 64,
                embedding_revision="rev",
                position=0,
                text=t,
                embedding=v,
            )
            for c, t, v in [
                ("a", "vacation days", [1.0, 0.0]),
                ("b", "holiday allowance", [0.8, 0.2]),
                ("c", "database incident", [0.0, 1.0]),
            ]
        ]
        gateway = Gateway(atlas_ports().url("gateway"))
        with (
            patch.object(gateway, "embed", return_value=([[1.0, 0.0]], "rev")),
            patch.object(
                gateway,
                "scores",
                return_value={"a" * 64: 1.0, "b" * 64: 2.0, "c" * 64: 0.0},
            ),
        ):
            result = rank("vacation", chunks, gateway, 2)
            self.assertEqual([c.doc_id for c in result], ["b", "a"])
            for question, k in [("", 2), ("question", 0), ("question", 9)]:
                with self.assertRaises(ValueError):
                    rank(question, chunks, gateway, k)
            self.assertEqual(rank("question", [], gateway), [])
        with patch.object(gateway, "embed", return_value=([[1.0, 0.0]], "changed")):
            with self.assertRaisesRegex(ValueError, "index_revision"):
                rank("q", chunks, gateway)

    def test_invalid_provider_vectors(self) -> None:
        gateway = Gateway(atlas_ports().url("gateway"))
        for vectors in [[[0.0]], [[float("nan")]], [[1.0], [2.0]]]:
            with patch.object(
                gateway, "post", return_value={"vectors": vectors, "revision": "rev"}
            ):
                with self.assertRaises(ValueError):
                    gateway.embed(["x"], "query")
        with patch.object(
            gateway, "post", return_value={"vectors": [[1.0]], "revision": "rev"}
        ):
            self.assertEqual(gateway.embed(["x"], "query"), ([[1.0]], "rev"))

    def test_http_and_score_validation(self) -> None:
        import io
        from email.message import Message
        from urllib.error import HTTPError, URLError

        gateway = Gateway(atlas_ports().url("gateway") + "/")
        with patch(
            "services.retrieval.search.urlopen", return_value=io.BytesIO(b'{"ok":true}')
        ):
            self.assertEqual(gateway.post("test", {}), {"ok": True})
        for error in [
            URLError("down"),
            TimeoutError(),
            HTTPError("url", 502, "bad", Message(), None),
        ]:
            with patch("services.retrieval.search.urlopen", side_effect=error):
                with self.assertRaises(ValueError):
                    gateway.post("test", {})
        with self.assertRaisesRegex(ValueError, "context_exceeded"):
            gateway.post("test", {"text": "x" * 800001})
        with patch(
            "services.retrieval.search.urlopen", return_value=io.BytesIO(b"x" * 800001)
        ):
            with self.assertRaisesRegex(ValueError, "provider_response_limit"):
                gateway.post("test", {})
        chunk = Chunk(
            chunk_id="a" * 64,
            doc_id="a",
            source="a.md",
            langue="en",
            source_sha256="b" * 64,
            embedding_revision="r",
            position=0,
            text="abc",
            embedding=[1.0],
        )
        score = {"chunk_id": chunk.chunk_id, "score": 1.0}
        for scores in [
            [],
            [score, score],
            [{"chunk_id": "c" * 64, "score": 1.0}],
            [{**score, "score": float("nan")}],
        ]:
            with patch.object(gateway, "post", return_value={"scores": scores}):
                with self.assertRaises(ValueError):
                    gateway.scores("q", [chunk])
        with patch.object(gateway, "post", return_value={"scores": [score]}):
            self.assertEqual(gateway.scores("q", [chunk]), {chunk.chunk_id: 1.0})
