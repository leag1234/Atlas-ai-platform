"""M11: the HTTP stream opens before upstream generation completes."""

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from starlette.types import Message

from packages.ports import atlas_ports
from services.orchestrator.chat_api import app
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction


class StreamingAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_headers_arrive_while_provider_is_still_generating(self) -> None:
        started, release, headers = asyncio.Event(), asyncio.Event(), asyncio.Event()
        body_sent = False
        payload = json.dumps(
            {"messages": [{"role": "user", "content": "Bonjour"}], "stream": True}
        ).encode()

        async def provider(request: ChatRequest, item: Interaction) -> None:
            started.set()
            await release.wait()
            item.state, item.reponse = "done", "Réponse synthétique"

        async def receive() -> dict[str, object]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            if message["type"] == "http.response.start":
                self.assertEqual(message["status"], 200)
                headers.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "server": ("127.0.0.1", atlas_ports().port("adapter")),
            "client": ("127.0.0.1", 42000),
        }
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch("services.orchestrator.chat_api.process", side_effect=provider),
        ):
            task = asyncio.create_task(app(scope, receive, send))
            try:
                await asyncio.wait_for(started.wait(), 1)
                try:
                    await asyncio.wait_for(headers.wait(), 0.2)
                except TimeoutError:
                    self.fail(
                        "SSE headers withheld until full generation; not progressive"
                    )
                self.assertFalse(release.is_set())
                self.assertFalse(task.done())
            finally:
                release.set()
                await asyncio.wait_for(task, 2)

    async def test_project_citations_and_memory_keep_their_scope(self) -> None:
        from services.orchestrator.chat_stream import response
        from services.orchestrator.stream_client import sink_context
        import time

        key, foreign = "a" * 64, "b" * 64
        request = ChatRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "Question"}],
                "project_id": "c" * 32,
                "conversation_id": "d" * 32,
            }
        )
        for citation, succeeds in ((key, True), (foreign, False)):
            item = Interaction()
            item.chunks_recuperes = [
                dict(
                    chunk_id=key, doc_id="doc", source="doc.md", text="preuve", score=1
                )
            ]
            memory = "Mémoire : " + foreign + "\n"

            async def process(payload: ChatRequest, entry: Interaction) -> None:
                sink = sink_context.get()
                assert sink is not None
                await sink({"delta": {"content": memory}, "memory": True})
                for part in ("Preuve ", citation[:20], citation[20:], "."):
                    await sink({"delta": {"content": part}})
                entry.state = "done"

            with (
                tempfile.TemporaryDirectory() as root,
                patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            ):
                stream = response(request, item, time.monotonic(), process)
                wire = "".join([str(chunk) async for chunk in stream.body_iterator])
            events = [
                json.loads(line[6:])
                for line in wire.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
            visible = "".join(
                e["choices"][0]["delta"].get("content", "")
                for e in events
                if "choices" in e
            )
            self.assertTrue(visible.startswith(memory))
            if succeeds:
                self.assertEqual(
                    visible,
                    memory
                    + f"Preuve [Source](/projects/{request.project_id}/sources/{key}).",
                )
                self.assertEqual(events[-1]["choices"][0]["finish_reason"], "stop")
            else:
                self.assertIn("error", events[-1])
                self.assertNotIn("/sources/" + foreign, visible)
                self.assertFalse(
                    any(
                        e.get("choices", [{}])[0].get("finish_reason") == "stop"
                        for e in events
                    )
                )
