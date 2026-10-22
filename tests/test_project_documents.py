"""Project document scopes, using synthetic local index vectors."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.retrieval.projects import Projects
from services.retrieval.project_documents import ProjectDocuments
from services.retrieval.search import Gateway


class ProjectDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.projects = Projects(Path(self.directory.name) / "projects.sqlite")
        self.first = self.projects.create("Premier", "")
        self.second = self.projects.create("Second", "")
        self.documents = ProjectDocuments(self.projects, Gateway("http://unused"))

    def test_source_resolution_requires_project(self) -> None:
        with patch.object(Gateway, "embed", return_value=([[1.0, 0.0]], "test")):
            self.documents.add(
                self.first, "Guide", "Le délai de livraison est de dix jours.", "fr"
            )
        chunks = self.documents.chunks(self.first)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(self.documents.chunks(self.second), [])
        self.assertEqual(
            self.documents.resolve(self.first, chunks[0].chunk_id), chunks[0]
        )
        with self.assertRaises(KeyError):
            self.documents.resolve(self.second, chunks[0].chunk_id)

    def test_identical_text_has_distinct_scope_ids(self) -> None:
        with patch.object(Gateway, "embed", return_value=([[1.0, 0.0]], "test")):
            for project in (self.first, self.second):
                self.documents.add(project, "Guide", "Texte identique.", "fr")
        a, b = (
            self.documents.chunks(self.first)[0],
            self.documents.chunks(self.second)[0],
        )
        self.assertNotEqual(a.chunk_id, b.chunk_id)
        self.assertNotEqual(a.doc_id, b.doc_id)

    def test_provider_failure_does_not_persist_partial_document(self) -> None:
        with patch.object(Gateway, "embed", side_effect=ValueError("provider_error")):
            with self.assertRaises(ValueError):
                self.documents.add(self.first, "Guide", "Texte.", "fr")
        self.assertEqual(self.documents.chunks(self.first), [])

    def test_unknown_project_rejected_before_provider_call(self) -> None:
        with patch.object(Gateway, "embed") as provider:
            with self.assertRaises(KeyError):
                self.documents.add("absent", "Guide", "Texte.", "fr")
            provider.assert_not_called()
