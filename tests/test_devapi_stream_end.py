"""Dev API stream termination telemetry."""

import json
from collections.abc import AsyncGenerator
from typing import Any, cast
import unittest
from unittest.mock import patch

from fastapi.responses import StreamingResponse
from starlette.requests import Request

from devapi_support import Environment
from services.orchestrator.dev_inference import chat
from test_dev_chat import BODY


class StreamEndTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.env = Environment()
        self.addCleanup(self.env.close)

    async def test_complete_stream_is_logged(self) -> None:
        with self.assertLogs("atlas.api", "WARNING") as logs:
            response = self.env.client.post(
                "/v1/chat/completions",
                headers=self.env.headers,
                json=BODY | {"stream": True},
            )

        self.assertEqual(response.status_code, 200, response.text)
        event = self.stream_end(logs)
        self.assertEqual(event["outcome"], "complete")
        self.assertEqual(event["protocol"], "chat")
        self.assertEqual(event["model"], "atlas-code")
        self.assertGreater(event["frames"], 0)
        self.assertGreater(event["bytes"], 0)
        self.assertGreaterEqual(event["elapsed_s"], 0)
        self.assertGreaterEqual(event["max_gap_s"], 0)

    async def test_provider_error_frame_is_logged(self) -> None:
        async def frames(plan: Any) -> Any:
            yield {"error": {"type": "provider_error", "message": "provider_error"}}

        with (
            patch(
                "services.orchestrator.dev_inference.gateway.events",
                side_effect=frames,
            ),
            self.assertLogs("atlas.api", "WARNING") as logs,
        ):
            response = self.env.client.post(
                "/v1/chat/completions",
                headers=self.env.headers,
                json=BODY | {"stream": True},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("provider_error", response.text)
        event = self.stream_end(logs)
        self.assertEqual(event["outcome"], "error_frame")
        self.assertEqual(event["error_type"], "provider_error")

    async def test_client_disconnect_is_logged(self) -> None:
        body = json.dumps(BODY | {"stream": True}).encode()

        async def receive() -> dict[str, Any]:
            return {
                "type": "http.request",
                "body": body,
                "more_body": False,
            }

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [(b"content-type", b"application/json")],
                "query_string": b"",
            }
        )
        request._receive = receive
        request.state.store = self.env.store
        request.state.developer = "alice"

        with self.assertLogs("atlas.api", "WARNING") as logs:
            response = cast(StreamingResponse, await chat(request))
            frames = cast(AsyncGenerator[str, None], response.body_iterator)
            await frames.__anext__()
            await frames.aclose()

        event = self.stream_end(logs)
        self.assertEqual(event["outcome"], "client_disconnect")
        self.assertEqual(event["protocol"], "chat")
        self.assertEqual(event["model"], "atlas-code")
        self.assertGreater(event["frames"], 0)
        self.assertGreater(event["bytes"], 0)

    def stream_end(self, logs: Any) -> dict[str, Any]:
        events = []
        for record in logs.output:
            try:
                event: dict[str, Any] = json.loads(record[record.index("{") :])
            except json.JSONDecodeError:
                continue
            if event.get("event") == "devapi_stream_end":
                events.append(event)
        self.assertEqual(len(events), 1)
        return events[0]
