"""Project scope must also govern requests whose client history contains images."""

import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from services.orchestrator.chat_api import app


class ProjectVisionScopeTests(unittest.TestCase):
    def test_prior_project_history_never_reaches_vision_after_switch(self) -> None:
        canary = "SYNTHETIC_ALPHA_SCOPE_CANARY"
        image = base64.b64encode(
            Path("tests/cassettes/vision/shapes.png").read_bytes()
        ).decode()
        upstream = AsyncMock(
            return_value={
                "text": "Description synthétique.",
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                "cost_eur": "0.001",
                "observation": {"provider": "escalade", "route": "complexe"},
            }
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Décris cette image pour le projet Alpha.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + image},
                    },
                ],
            },
            {"role": "assistant", "content": canary},
            {"role": "user", "content": "/project use Beta"},
            {
                "role": "assistant",
                "content": "Project selected: Beta. [atlas-project:00000000-0000-0000-0000-000000000001:00000000-0000-0000-0000-000000000002]",
            },
            {"role": "user", "content": "Quel est le code de ce projet ?"},
        ]
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch("services.orchestrator.model.GatewayModel.post", new=upstream),
            TestClient(app) as client,
        ):
            client.post("/v1/chat/completions", json={"messages": messages})
        forwarded = [
            call.args[1]
            for call in upstream.await_args_list
            if call.args[0].endswith("/vision/complete")
        ]
        self.assertNotIn(canary, json.dumps(forwarded))

    def test_current_upload_uses_server_history_and_persists_description(self) -> None:
        image = base64.b64encode(
            Path("tests/cassettes/vision/shapes.png").read_bytes()
        ).decode()
        project = "00000000-0000-0000-0000-000000000001"
        conversation = "00000000-0000-0000-0000-000000000002"

        async def post(
            url: str, payload: dict[str, Any], timeout: float
        ) -> dict[str, Any]:
            if url.endswith("/context"):
                return {
                    "instructions": "",
                    "revision": 0,
                    "facts": [],
                    "history": [
                        {"question": "Bonjour Beta", "answer": "Bienvenue Beta"}
                    ],
                }
            if url.endswith("/vision/complete"):
                self.assertNotIn("ALPHA_CANARY", json.dumps(payload))
                self.assertIn("Bonjour Beta", json.dumps(payload))
                self.assertIn("data:image/png;base64,", json.dumps(payload))
                return {
                    "text": "Un carré rouge et un cercle bleu.",
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                    "cost_eur": "0.001",
                    "observation": {"provider": "escalade", "route": "complexe"},
                }
            self.assertTrue(url.endswith(f"/projects/{project}/turns"))
            self.assertEqual(payload["conversation_id"], conversation)
            self.assertEqual(payload["facts"], [])
            self.assertNotIn("base64", json.dumps(payload))
            return {}

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch(
                "services.orchestrator.model.GatewayModel.post", side_effect=post
            ) as upstream,
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "project_id": project,
                    "conversation_id": conversation,
                    "messages": [
                        {"role": "user", "content": "ALPHA_CANARY"},
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Décris cette image."},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "data:image/png;base64," + image,
                                    },
                                },
                            ],
                        },
                    ],
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "carré rouge", response.json()["choices"][0]["message"]["content"]
        )
        self.assertEqual(upstream.await_count, 3)
