"""Synthetic integration through real project HTTP routes and chat orchestration."""

import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Message
from services.orchestrator.model import Configuration, GatewayModel
from services.retrieval.api import app


class ProjectChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(
            os.environ,
            {
                "ATLAS_PROJECT_DB": str(Path(self.directory.name) / "projects.sqlite"),
                "ATLAS_WEB_CACHE": str(Path(self.directory.name) / "web.sqlite"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app)
        self.first = self.client.post("/projects", json={"name": "Premier"}).json()[
            "id"
        ]
        self.second = self.client.post("/projects", json={"name": "Autre"}).json()["id"]
        self.seen: list[Message] = []
        self.generated = 0

    async def test_project_model_receives_resolved_language(self) -> None:
        await self.ask(
            self.first, self.conversation(self.first), "Bonjour, discutons en français."
        )
        calls = [
            row for row in self.seen if str(row["url"]).endswith("/agent/complete")
        ]
        self.assertIn("Answer in French.", str(calls[0]["messages"]))

    def conversation(self, project: str) -> str:
        return str(
            self.client.post(
                f"/projects/{project}/conversations", json={"name": "Conversation"}
            ).json()["id"]
        )

    async def post(self, url: str, payload: Message, timeout: float) -> Message:
        self.seen.append({"url": url, **payload})
        if "/projects/" in url:
            path = "/projects/" + url.split("/projects/", 1)[1]
            response = self.client.post(path, json=payload)
            if response.status_code != 200:
                raise RuntimeError("project_unavailable")
            result = response.json()
            assert isinstance(result, dict)
            return result
        self.assertTrue(url.endswith("/agent/complete"))
        self.generated += 1
        messages = payload["messages"]
        assert isinstance(messages, list)
        question = json.loads(messages[-1]["content"])["question"]
        system = str(messages[0]["content"])
        facts = (
            [{"text": "Le projet utilise Python.", "evidence": "utilise Python"}]
            if question == "Mon projet utilise Python."
            else []
        )
        answer = (
            "Le langage est Python, selon ta mémoire."
            if "Le projet utilise Python." in system
            else "Je ne connais pas le langage."
        )
        return {
            "text": json.dumps({"answer": answer, "facts": facts}),
            "calls": [],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            "cost_eur": "0.0001",
            "observation": {"provider": "escalade", "route": "complexe"},
        }

    async def ask(self, project: str, conversation: str, question: str) -> Interaction:
        request = ChatRequest(
            project_id=project,
            conversation_id=conversation,
            messages=[ChatMessage(role="user", content=question)],
        )
        item = Interaction()
        model = GatewayModel(
            "http://gateway",
            Configuration(
                input_eur_per_mtok=Decimal("0.5"),
                output_eur_per_mtok=Decimal("1"),
                max_tokens=2048,
            ),
        )
        with (
            patch.object(GatewayModel, "connect", return_value=model) as connect,
            patch.object(GatewayModel, "post", side_effect=self.post),
        ):
            await process(request, item)
            self.assertFalse(connect.call_args.kwargs["local_enabled"])
        return item

    async def test_reuse_isolation_and_erasure(self) -> None:
        original = self.conversation(self.first)
        await self.ask(self.first, original, "Mon projet utilise Python.")
        other = self.conversation(self.first)
        reused = await self.ask(self.first, other, "Quel langage utilise le projet ?")
        self.assertEqual(reused.state, "done")
        self.assertIn("Python", reused.reponse)
        self.assertIn("Project memory", reused.reponse)
        self.assertLess(reused.cout_eur, 0.05)
        isolated = await self.ask(
            self.second, self.conversation(self.second), "Quel langage ?"
        )
        self.assertNotIn("Python", isolated.reponse)
        self.client.delete(f"/projects/{self.first}/facts")
        erased = await self.ask(self.first, original, "Quel langage ?")
        self.assertNotIn("Python", erased.reponse)
        self.assertEqual(self.client.get(f"/projects/{self.first}/facts").json(), [])
        self.assertFalse(
            any("web_search" in str(call.get("tools", [])) for call in self.seen)
        )

    async def test_foreign_conversation_stops_before_generation(self) -> None:
        with self.assertRaises(RuntimeError):
            await self.ask(self.second, self.conversation(self.first), "Question")
        self.assertEqual(self.generated, 0)

    async def test_budget_stops_before_generation(self) -> None:
        model = GatewayModel(
            "http://gateway",
            Configuration(
                input_eur_per_mtok=Decimal("100"),
                output_eur_per_mtok=Decimal("100"),
                max_tokens=2048,
            ),
        )
        request = ChatRequest(
            project_id=self.first,
            conversation_id=self.conversation(self.first),
            messages=[ChatMessage(role="user", content="Question")],
        )
        item = Interaction()
        with (
            patch.object(GatewayModel, "connect", return_value=model),
            patch.object(GatewayModel, "post", side_effect=self.post),
        ):
            await process(request, item)
        self.assertEqual(self.generated, 0)
        self.assertIn("cost", item.erreurs)

    def test_project_ids_are_paired_and_validated(self) -> None:
        for values in (
            {"project_id": self.first},
            {"project_id": "../x", "conversation_id": "../y"},
        ):
            with self.assertRaises(ValueError):
                ChatRequest.model_validate(
                    {**values, "messages": [{"role": "user", "content": "Question"}]}
                )

    async def test_scoped_passages_fit_serverless_context(self) -> None:
        from services.orchestrator.project_chat import ProjectTools
        from services.orchestrator.loop import Call

        passages = [
            dict(
                chunk_id=f"{i:064x}",
                doc_id=str(i),
                source="doc.md",
                text="é" * 500,
                score=float(i),
            )
            for i in range(8)
        ]
        tools = ProjectTools(Interaction(), self.first)
        with patch.object(
            GatewayModel, "post", return_value={"passages": passages}
        ) as post:
            output = await tools.execute(Call("c", "rag_search", '{"query":"test"}'), 5)
        self.assertIn(f"/projects/{self.first}/search", post.call_args.args[0])
        data = output["data"]
        assert isinstance(data, dict)
        self.assertLessEqual(
            len(json.dumps(data["passages"], ensure_ascii=False).encode()), 4096
        )
        self.assertEqual(len(tools.item.chunks_recuperes), 8)
