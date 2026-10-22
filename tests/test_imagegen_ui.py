"""Synthetic adapter requests prove image delivery and redacted observations."""

import json
import base64
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from services.orchestrator.chat_api import app
from test_imagegen import png
from decimal import Decimal
from services.orchestrator.model import GatewayModel, Configuration
from services.orchestrator.loop import Turn, Call, Reservation


class ImageUITests(unittest.TestCase):
    def test_chat_and_sse_return_image_without_logging_bytes(self) -> None:
        url = png()
        upstream = AsyncMock(return_value={"image": url, "cost_eur": 0.001})
        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=100,
            ),
        )
        decision = AsyncMock(
            return_value=Turn(
                "",
                (
                    Call(
                        "image",
                        "generate_image",
                        json.dumps({"prompt": "dessine-moi un mouton"}),
                    ),
                ),
                Reservation(10, Decimal(0)),
            )
        )
        with (
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(model, "complete", decision),
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch("services.orchestrator.model.GatewayModel.post", new=upstream),
            TestClient(app) as client,
        ):
            for stream in (False, True):
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "messages": [
                            {"role": "user", "content": "dessine-moi un mouton"}
                        ],
                        "stream": stream,
                    },
                )
                self.assertEqual(response.status_code, 200)
                if stream:
                    self.assertIn("data: [DONE]", response.text)
                    events = [
                        json.loads(line[6:])
                        for line in response.text.splitlines()
                        if line.startswith("data: ") and line != "data: [DONE]"
                    ]
                    self.assertFalse(any("error" in e for e in events))
                    text = "".join(
                        e["choices"][0]["delta"].get("content", "") for e in events
                    )
                else:
                    text = response.json()["choices"][0]["message"]["content"]
                self.assertNotIn("base64", text)
                reference = text.partition("](")[2].removesuffix(")")
                self.assertEqual(text, f"![Image générée]({reference})")
                image_response = client.get(reference)
                self.assertEqual(image_response.status_code, 200)
                self.assertEqual(
                    image_response.content, base64.b64decode(url.split(",", 1)[1])
                )
            self.assertEqual(upstream.await_count, 2)
            assert upstream.await_args is not None
            self.assertTrue(upstream.await_args.args[0].endswith("/images/generate"))
            rows = [
                json.loads(line)
                for p in Path(root).glob("*.jsonl")
                for line in p.read_text().splitlines()
            ]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(r["task_type"] == "imagegen" for r in rows))
            self.assertTrue(all(r["cout_eur"] == 0.001 for r in rows))
            self.assertNotIn(url.split(",", 1)[1], json.dumps(rows))
