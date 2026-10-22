"""HTTP project boundaries reject foreign IDs and untrusted browser origins."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from services.retrieval.api import app


class ProjectAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(
            os.environ,
            {"ATLAS_PROJECT_DB": str(Path(self.directory.name) / "projects.sqlite")},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(app)

    def project(self, name: str) -> str:
        response = self.client.post(
            "/projects", json={"name": name, "instructions": ""}
        )
        self.assertEqual(response.status_code, 200)
        return str(response.json()["id"])

    def test_create_list_and_validate(self) -> None:
        project = self.project("Projet français")
        self.assertEqual(self.client.get("/projects").json()[0]["id"], project)
        for invalid in ("", " ", "x" * 121):
            self.assertEqual(
                self.client.post("/projects", json={"name": invalid}).status_code, 422
            )

    def test_foreign_conversation_is_not_readable(self) -> None:
        first, second = self.project("Premier"), self.project("Second")
        conversation = self.client.post(
            f"/projects/{first}/conversations", json={"name": "A"}
        ).json()["id"]
        response = self.client.get(f"/projects/{second}/conversations/{conversation}")
        self.assertEqual(response.status_code, 404)
        response = self.client.post(
            f"/projects/{second}/context",
            json={"conversation_id": conversation, "query": "Question"},
        )
        self.assertEqual(response.status_code, 404)

    def test_erase_and_inspect(self) -> None:
        first = self.project("Premier")
        conversation = self.client.post(
            f"/projects/{first}/conversations", json={"name": "A"}
        ).json()["id"]
        payload = {
            "conversation_id": conversation,
            "question": "Je préfère Python.",
            "answer": "Compris.",
            "facts": ["Je préfère Python."],
            "revision": 0,
        }
        self.assertEqual(
            self.client.post(f"/projects/{first}/turns", json=payload).status_code, 200
        )
        facts = self.client.get(f"/projects/{first}/facts").json()
        self.assertEqual(len(facts), 1)
        fact = facts[0]["id"]
        self.assertEqual(
            self.client.patch(
                f"/projects/{first}/facts/{fact}", json={"text": "Je préfère Rust."}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(f"/projects/{first}/facts").json()[0]["text"],
            "Je préfère Rust.",
        )
        self.assertEqual(
            self.client.delete(f"/projects/{first}/facts").status_code, 200
        )
        self.assertEqual(self.client.get(f"/projects/{first}/facts").json(), [])

    def test_error_response_does_not_reflect_input(self) -> None:
        response = self.client.get("/projects/private-sentinel")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("private-sentinel", response.text)

    def test_project_instructions_documents_and_scoped_search(self) -> None:
        from services.retrieval.search import Gateway

        project, other = self.project("Projet"), self.project("Autre")
        root = f"/projects/{project}"
        self.assertEqual(
            self.client.patch(
                root, json={"name": "Modifié", "instructions": "Répondre brièvement."}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(root).json()["instructions"], "Répondre brièvement."
        )
        self.assertEqual(self.client.get(root + "/conversations").json(), [])
        with patch.object(Gateway, "embed", return_value=([[1.0, 0.0]], "test")):
            added = self.client.post(
                root + "/documents",
                json={"name": "Note", "text": "Délai de dix jours.", "lang": "fr"},
            )
        self.assertEqual(added.status_code, 200)
        self.assertEqual(len(self.client.get(root + "/documents").json()), 1)
        from services.retrieval.project_documents import ProjectDocuments
        from services.retrieval.projects import Projects

        documents = ProjectDocuments(
            Projects(Path(os.environ["ATLAS_PROJECT_DB"])), Gateway("http://unused")
        )
        key = documents.chunks(project)[0].chunk_id
        with (
            patch.object(Gateway, "embed", return_value=([[1.0, 0.0]], "test")),
            patch.object(Gateway, "scores", return_value={key: 1.0}),
        ):
            response = self.client.post(root + "/search", json={"query": "Délai ?"})
        self.assertEqual(response.json()["passages"][0]["chunk_id"], key)
        self.assertEqual(self.client.get(root + "/sources/" + key).status_code, 200)
        self.assertEqual(
            self.client.get(f"/projects/{other}/sources/{key}").status_code, 404
        )
