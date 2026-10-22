"""Document citations and provider failures exercise the production project path."""

import json
from unittest.mock import patch

from services.orchestrator.loop import Message
from services.retrieval.project_documents import ProjectDocuments
from services.retrieval.projects import Projects
from services.retrieval.search import Gateway
from test_project_chat import ProjectChatTests
import os
from pathlib import Path


class ProjectFailureTests(ProjectChatTests):
    async def test_scoped_document_tool_and_citation(self) -> None:
        documents = ProjectDocuments(
            Projects(Path(os.environ["ATLAS_PROJECT_DB"])), Gateway("http://unused")
        )
        with patch.object(Gateway, "embed", return_value=([[1.0, 0.0]], "test")):
            documents.add(self.first, "Note", "Le délai est dix jours.", "fr")
        key = documents.chunks(self.first)[0].chunk_id
        ordinary = self.post
        count = 0

        async def provider(url: str, payload: Message, timeout: float) -> Message:
            nonlocal count
            if "/agent/complete" not in url:
                return await ordinary(url, payload, timeout)
            count += 1
            calls = (
                [
                    {
                        "id": "retrieve",
                        "name": "rag_search",
                        "arguments": json.dumps({"query": "Délai ?"}),
                    }
                ]
                if count == 1
                else []
            )
            return {
                "text": ""
                if calls
                else json.dumps({"answer": "Dix jours [" + key + "].", "facts": []}),
                "calls": calls,
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                "cost_eur": "0.0001",
                "observation": {"provider": "escalade", "route": "complexe"},
            }

        with (
            patch.object(self, "post", side_effect=provider),
            patch.object(Gateway, "embed", return_value=([[1.0, 0.0]], "test")),
            patch.object(Gateway, "scores", return_value={key: 1.0}),
        ):
            response = await self.ask(
                self.first, self.conversation(self.first), "Quel délai ?"
            )
        self.assertEqual(count, 2)
        self.assertIn(f"/projects/{self.first}/sources/{key}", response.reponse)
        self.assertEqual(response.citations[0]["chunk_id"], key)

    async def test_malformed_consolidation_never_persists(self) -> None:
        ordinary = self.post

        async def provider(url: str, payload: Message, timeout: float) -> Message:
            result = await ordinary(url, payload, timeout)
            if "/agent/complete" in url:
                result["text"] = (
                    '{"answer":"ok","facts":[{"text":"secret inventé","evidence":"absent"}]}'
                )
            return result

        conversation = self.conversation(self.first)
        with (
            patch.object(self, "post", side_effect=provider),
            self.assertRaises(ValueError),
        ):
            await self.ask(self.first, conversation, "Bonjour")
        self.assertEqual(self.client.get(f"/projects/{self.first}/facts").json(), [])
        self.assertEqual(
            self.client.get(
                f"/projects/{self.first}/conversations/{conversation}"
            ).json(),
            [],
        )
