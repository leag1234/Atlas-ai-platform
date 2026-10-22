"""Internal HTTP cancels a stalled upstream on deadline and peer disconnect."""

import asyncio
from collections.abc import AsyncIterator
import os
import threading
import unittest
from unittest.mock import Mock, patch

import aiohttp
from serverless_support import environment
from agent_provider import AgentProvider
from gateway_cpu import CPUModels
from http_gateway import serve


class GatewayStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_upstream_closes_on_deadline_and_disconnect(self) -> None:
        for disconnect in (False, True):
            with self.subTest(disconnect=disconnect):
                closed = threading.Event()

                async def provider(
                    instance: AgentProvider, payload: object
                ) -> AsyncIterator[dict[str, object]]:
                    try:
                        yield {"delta": {"content": "début"}}
                        await asyncio.Event().wait()
                    finally:
                        closed.set()

                with (
                    patch.dict(os.environ, environment()),
                    patch.object(AgentProvider, "stream", provider),
                ):
                    gateway = serve(Mock(spec=CPUModels), 0)
                    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
                    thread.start()
                    try:
                        async with aiohttp.ClientSession(
                            timeout=aiohttp.ClientTimeout(total=1)
                        ) as client:
                            async with client.post(
                                f"http://127.0.0.1:{gateway.server_port}/agent/stream",
                                json={
                                    "messages": [{"role": "user", "content": "test"}],
                                    "tools": [{}],
                                    "timeout": 0.1,
                                },
                            ) as response:
                                self.assertEqual(response.status, 200)
                                first = await response.content.readline()
                                self.assertIn(b"delta", first)
                                if not disconnect:
                                    body = await response.read()
                                    self.assertIn(b"provider_error", body)
                        self.assertTrue(await asyncio.to_thread(closed.wait, 0.5))
                    finally:
                        await asyncio.to_thread(gateway.shutdown)
                        gateway.server_close()
                        await asyncio.to_thread(thread.join, 2)
