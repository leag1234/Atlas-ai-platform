import unittest
from unittest.mock import Mock

from services.retrieval.answer import answer
from services.retrieval.evaluate import Case, evaluate, metrics
from services.retrieval.search import Gateway
from services.retrieval.store import Chunk, Store


class EvaluationTests(unittest.TestCase):
    def test_metrics_include_misses(self) -> None:
        self.assertEqual(
            metrics([1, 2, 0, 8]), {"cases": 4, "recall_at_8": 0.75, "mrr": 0.40625}
        )
        with self.assertRaises(ValueError):
            metrics([])

    def test_resolution_and_rejections(self) -> None:
        chunk = Chunk(
            chunk_id="a" * 64,
            doc_id="doc",
            langue="fr",
            source="doc.md",
            source_sha256="b" * 64,
            embedding_revision="test",
            position=0,
            text="source",
            embedding=[1.0],
        )
        gateway = Mock(spec=Gateway)
        store = Mock(spec=Store)
        store.resolve.return_value = chunk
        good = {
            "text": "Fact [" + chunk.chunk_id + "].",
            "citations": [chunk.chunk_id],
            "refused": False,
        }
        gateway.post.return_value = good
        self.assertEqual(
            answer("q", [chunk], gateway, store).citations, [chunk.chunk_id]
        )
        changes: list[dict[str, object]] = [
            {"citations": []},
            {"citations": ["c" * 64]},
            {"refused": True},
            {"text": "no reference"},
            {"citations": [chunk.chunk_id] * 2},
        ]
        for change in changes:
            gateway.post.return_value = good | change
            with self.assertRaises(ValueError):
                answer("q", [chunk], gateway, store)
        gateway.post.return_value = good
        store.resolve.side_effect = KeyError("deleted")
        with self.assertRaises(KeyError):
            answer("q", [chunk], gateway, store)
        store.resolve.side_effect = None
        store.resolve.return_value = chunk.model_copy(update={"text": "changed"})
        with self.assertRaisesRegex(ValueError, "citation_changed"):
            answer("q", [chunk], gateway, store)

    def test_evaluation_uses_question_and_requires_real_citations(self) -> None:
        from unittest.mock import patch

        chunk = Chunk(
            chunk_id="a" * 64,
            doc_id="doc",
            langue="fr",
            source="doc.md",
            source_sha256="b" * 64,
            embedding_revision="test",
            position=0,
            text="expected",
            embedding=[1.0],
        )
        cases = [
            Case(
                id=str(i),
                lang="fr",
                input="question",
                doc_id_attendu="doc",
                passage_attendu="expected",
            )
            for i in range(7)
        ]
        store = Mock(spec=Store)
        store.read.return_value = [chunk]
        store.resolve.return_value = chunk
        gateway = Mock(spec=Gateway)
        gateway.post.return_value = {
            "text": "fact [" + chunk.chunk_id + "]",
            "citations": [chunk.chunk_id],
            "refused": False,
        }
        with patch("services.retrieval.evaluate.rank", return_value=[chunk]) as search:
            report = evaluate(cases, store, gateway)
            self.assertEqual(report["recall_at_8"], 1.0)
            self.assertTrue(report["citations_resolues"])
            self.assertTrue(
                all(call.args[0] == "question" for call in search.call_args_list)
            )
            cases[0].doc_id_attendu = "wrong-document"
            self.assertEqual(evaluate(cases, store, gateway)["recall_at_8"], 6 / 7)
            gateway.post.return_value = {
                "text": "not found",
                "citations": [],
                "refused": True,
            }
            with self.assertRaisesRegex(ValueError, "citations_missing"):
                evaluate(cases, store, gateway)

    def test_gate_cleans_up_after_initialization_failure(self) -> None:
        from unittest.mock import patch
        from m2_gate import main

        with (
            patch("m2_gate.StorageTests.setUpClass", side_effect=RuntimeError("init")),
            patch("m2_gate.StorageTests.doClassCleanups") as cleanup,
        ):
            with self.assertRaisesRegex(RuntimeError, "init"):
                main()
            cleanup.assert_called_once()
