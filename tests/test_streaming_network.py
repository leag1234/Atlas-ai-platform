"""M11: real HTTP on both internal boundaries, provider barrier proves progress."""

import asyncio
from collections.abc import AsyncIterator
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import uvicorn
from serverless_support import environment
from agent_provider import AgentProvider
from gateway_cpu import CPUModels
from http_gateway import serve
from services.orchestrator.chat_api import app
from services.orchestrator.interactions import Interaction, write_interaction


class StreamingNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_content_reaches_client_before_upstream_second_piece(
        self,
    ) -> None:
        release, entered, closed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        journalled = threading.Event()

        def write_and_signal(item: Interaction, directory: Path) -> None:
            write_interaction(item, directory)
            journalled.set()

        observed: list[dict[str, Any]] = []

        async def provider(
            instance: AgentProvider, payload: object
        ) -> AsyncIterator[dict[str, object]]:
            entered.set()
            try:
                yield {"delta": {"content": "Bon"}}
                while not release.is_set():
                    await asyncio.sleep(0.01)
                yield {"delta": {"content": "jour"}}
                yield {
                    "result": {
                        "text": "Bonjour",
                        "calls": [],
                        "usage": {"prompt_tokens": 40, "completion_tokens": 2},
                        "cost_eur": "0.0000312",
                        "observation": {"provider": "escalade", "route": "complexe"},
                    }
                }
            finally:
                closed.set()

        configuration = {
            "input_eur_per_mtok": "0.6",
            "output_eur_per_mtok": "3.6",
            "max_tokens": 2048,
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    **environment(),
                    "GPU_LOCAL": "0",
                    "ATLAS_INTERACTION_DIR": directory,
                    "ATLAS_WEB_CACHE": str(Path(directory) / "cache.sqlite"),
                },
            ),
            patch(
                "services.orchestrator.chat_stream.write_interaction",
                side_effect=write_and_signal,
            ),
            patch.object(AgentProvider, "configuration", return_value=configuration),
            patch.object(AgentProvider, "stream", provider, create=True),
            patch.object(
                AgentProvider,
                "complete",
                new=AsyncMock(
                    side_effect=RuntimeError("atomic_path_used_instead_of_stream")
                ),
            ),
        ):
            gateway = serve(Mock(spec=CPUModels), port=0)
            gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
            gateway_thread.start()
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = sock.getsockname()[1]
            server = uvicorn.Server(
                uvicorn.Config(app, log_config=None, access_log=False, lifespan="off")
            )
            thread = threading.Thread(
                target=lambda: server.run(sockets=[sock]), daemon=True
            )
            with patch.dict(
                os.environ,
                {"ATLAS_GATEWAY_URL": f"http://127.0.0.1:{gateway.server_port}"},
            ):
                thread.start()
                try:
                    async with asyncio.timeout(5):
                        while not server.started:
                            await asyncio.sleep(0.01)
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=5), trust_env=False
                    ) as client:
                        async with client.post(
                            f"http://127.0.0.1:{port}/v1/chat/completions",
                            json={
                                "stream": True,
                                "messages": [{"role": "user", "content": "Bonjour"}],
                            },
                        ) as response:
                            self.assertEqual(response.status, 200)
                            self.assertIn(
                                "text/event-stream", response.headers["Content-Type"]
                            )
                            done = False
                            async for line in response.content:
                                if not line.startswith(b"data:"):
                                    continue
                                data = line[5:].strip()
                                if data == b"[DONE]":
                                    done = True
                                    break
                                event = json.loads(data)
                                self.assertNotIn("error", event)
                                observed.append(event)
                                for choice in event.get("choices", []):
                                    content = choice.get("delta", {}).get("content")
                                    if content and not release.is_set():
                                        self.assertEqual(content, "Bon")
                                        self.assertTrue(entered.is_set())
                                        self.assertFalse(closed.is_set())
                                        release.set()
                            self.assertTrue(done)
                    self.assertTrue(
                        release.is_set(),
                        "No content received before upstream completion",
                    )
                    self.assertTrue(closed.is_set())
                    deltas = [
                        c["delta"]["content"]
                        for event in observed
                        for c in event.get("choices", [])
                        if c.get("delta", {}).get("content")
                    ]
                    self.assertEqual(deltas, ["Bon", "jour"])
                    # DONE can reach the client before the server generator's
                    # finally block writes telemetry. Await that real write.
                    self.assertTrue(await asyncio.to_thread(journalled.wait, 5))
                    rows = [
                        json.loads(line)
                        for path in Path(directory).glob("*.jsonl")
                        for line in path.read_text().splitlines()
                    ]
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["reponse"], "Bonjour")
                    self.assertLessEqual(rows[0]["cout_eur"], 0.05)
                    self.assertLess(rows[0]["latence_ms"]["total"], 120000)
                    if os.environ.get("ATLAS_STREAM_REPORT"):
                        report = Path(os.environ["ATLAS_STREAM_REPORT"])
                        report.parent.mkdir(parents=True, exist_ok=True)
                        report.write_text(
                            json.dumps(
                                {
                                    "chunk_count": len(deltas),
                                    "received_before_upstream_finished": release.is_set(),
                                    "upstream_closed": closed.is_set(),
                                    "cost_eur": rows[0]["cout_eur"],
                                    "latency_ms": rows[0]["latence_ms"]["total"],
                                    "transport": "real HTTP, controlled provider barrier",
                                }
                            )
                        )
                finally:
                    release.set()
                    server.should_exit = True
                    await asyncio.to_thread(thread.join, 3)
                    await asyncio.to_thread(gateway.shutdown)
                    gateway.server_close()
                    await asyncio.to_thread(gateway_thread.join, 3)
                    sock.close()
                    self.assertFalse(thread.is_alive())
                    self.assertFalse(gateway_thread.is_alive())
