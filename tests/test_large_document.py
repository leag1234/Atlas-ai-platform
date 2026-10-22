"""M10 regression: chat must keep evidence at the end of a retrieved chunk."""

import json
import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator.chat_pipeline import ChatTools
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call
from services.orchestrator.model import GatewayModel


class LargeDocumentTests(unittest.IsolatedAsyncioTestCase):
    async def test_tail_of_document_passage_reaches_model(self) -> None:
        text = "Routine information without the requested fact. " * 400
        text += "\nZephyr warranty lasts 73 months."
        passage = {
            "chunk_id": "a" * 64,
            "doc_id": "long-document",
            "source": "long-document.md",
            "text": text,
            "score": 1.0,
        }
        item = Interaction()
        with patch.object(
            GatewayModel, "post", new=AsyncMock(return_value={"passages": [passage]})
        ):
            result = await ChatTools(item).execute(
                Call("1", "rag_search", json.dumps({"query": "Zephyr warranty"})), 5
            )
        self.assertIn("73 months", json.dumps(result))
        self.assertIn("a" * 64, json.dumps(result))
        self.assertEqual(item.chunks_recuperes, [passage])
        self.assertEqual(result["trust"], "untrusted")
        self.assertEqual(item.erreurs, [])
