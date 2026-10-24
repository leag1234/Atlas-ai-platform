"""Regression from the L4 captured Orion answer and scoped SSE finalization."""

import json
import time
import unittest
from unittest.mock import patch

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.interactions import Interaction
from services.orchestrator.project_citations import render
from services.retrieval.project_identity import ProjectError

A = "664f1c827728b3c875a89d7b534b3bfb4a470db868123efb4ea17e93bab11517"
B = "ecb73b3bec996344164df1ec15df4f16ccbc1bbe6b4613083e9fb449e20238a0"
ORG = "07a8c69f-eed4-4935-926b-161de581488d"
PROJECT = "d31e498c-7095-4fd2-9aef-355d6d93f788"
PASSAGES = [dict(chunk_id=A), dict(chunk_id=B)]
RAW = f"The Orion project involves 59 sites. Delivery is 15 December 2026 [[{A}]({A}), ({B})({B})]."


class ScopedCitationTests(unittest.IsolatedAsyncioTestCase):
    def test_captured_markdown_and_duplicate_references(self) -> None:
        result = render(RAW, PASSAGES, ORG, PROJECT)
        self.assertIn(
            f"[1](/api/atlas/orgs/{ORG}/projects/{PROJECT}/sources/{A})", result
        )
        self.assertIn(
            f"[2](/api/atlas/orgs/{ORG}/projects/{PROJECT}/sources/{B})", result
        )
        self.assertNotIn("[Source]", result)
        self.assertNotIn("]([", result)
        self.assertEqual(render(result, PASSAGES, ORG, PROJECT), result)

    def test_foreign_reference_fails_closed(self) -> None:
        for raw in ("c" * 64, f"[{'c' * 64}]({A})"):
            with self.assertRaises(ProjectError):
                render(raw, PASSAGES, ORG, PROJECT)

    async def test_canonical_scoped_stream_does_not_reenter_legacy_renderer(
        self,
    ) -> None:
        request = ChatRequest.model_validate(
            dict(
                org_id=ORG,
                project_id=PROJECT,
                conversation_id="11111111-1111-4111-8111-111111111111",
                messages=[dict(role="user", content="Summarize both sources")],
                stream=True,
            )
        )
        expected = render(RAW, PASSAGES, ORG, PROJECT)

        async def process(payload: ChatRequest, item: Interaction) -> None:
            item.reponse, item.state = expected, "done"

        with patch("services.orchestrator.chat_stream.write_interaction"):
            wire = "".join(
                [
                    str(c)
                    async for c in response(
                        request, Interaction(), time.monotonic(), process
                    ).body_iterator
                ]
            )
        events = [
            json.loads(line[6:])
            for line in wire.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        self.assertFalse(any("error" in e for e in events))
        visible = "".join(
            e["choices"][0]["delta"].get("content", "")
            for e in events
            if "choices" in e
        )
        self.assertEqual(visible, expected)
        self.assertEqual(events[-1]["choices"][0]["finish_reason"], "stop")
