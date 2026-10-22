"""M7 retrieval boundary: expose actual scores and resolve owned citations."""

import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from services.retrieval.api import app
from services.retrieval.store import Chunk


class ChatRetrievalTests(unittest.TestCase):
    def test_search_and_resolution(self) -> None:
        chunk = Chunk(
            chunk_id="a" * 64,
            doc_id="doc",
            source="doc.md",
            langue="fr",
            source_sha256="b" * 64,
            embedding_revision="test",
            position=0,
            text="preuve",
            embedding=[1.0],
        )
        store = Mock()
        store.read.return_value = [chunk]
        store.resolve.return_value = chunk
        with (
            TestClient(app) as client,
            patch("services.retrieval.api.store", return_value=store),
            patch("services.retrieval.api.rank_scored", return_value=[(chunk, 0.75)]),
        ):
            response = client.post("/search", json={"query": "preuve"})
            self.assertEqual(response.status_code, 200)
            passage = response.json()["passages"][0]
            self.assertEqual(passage["score"], 0.75)
            self.assertEqual(passage["doc_id"], "doc")
            self.assertNotIn("embedding", passage)
            source = client.get("/sources/" + chunk.chunk_id)
            self.assertEqual(source.json()["text"], chunk.text)
            store.resolve.side_effect = KeyError("chunk_not_found")
            self.assertEqual(client.get("/sources/" + "b" * 64).status_code, 404)
            self.assertEqual(client.get("/sources/invalid").status_code, 400)
            for text in ("", " ", "x" * 4001):
                self.assertEqual(
                    client.post("/search", json={"query": text}).status_code, 422
                )
            store.read.side_effect = RuntimeError("private provider detail")
            response = client.post("/search", json={"query": "preuve"})
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private", response.text)
