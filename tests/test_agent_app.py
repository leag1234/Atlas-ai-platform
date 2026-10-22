"""HTTP validation and explicit gateway failure without external network."""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from services.orchestrator.app import app
from services.orchestrator.cache import Cache
from services.orchestrator.loop import Result


class AppTests(unittest.TestCase):
    def test_invalid_request_and_gateway_failure(self) -> None:
        with TestClient(app) as client:
            self.assertEqual(
                client.post("/query", json={"question": " ", "lang": "fr"}).status_code,
                422,
            )
            with patch(
                "services.orchestrator.app.GatewayModel.connect",
                new=AsyncMock(side_effect=RuntimeError),
            ):
                self.assertEqual(
                    client.post(
                        "/query", json={"question": "hello", "lang": "en"}
                    ).status_code,
                    503,
                )

    def test_response_and_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as directory, TestClient(app) as client:
            store = Cache(Path(directory) / "cache.sqlite")
            store.put(
                "a" * 64, {"text": "x" * 9000, "url": "https://example.org"}, 3600
            )
            with patch("services.orchestrator.app.cache", return_value=store):
                response = client.get("/continuation/" + "a" * 64)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["next_offset"], 8000)
                self.assertEqual(client.get("/continuation/bad").status_code, 400)
                self.assertEqual(
                    client.get("/continuation/" + "b" * 64).status_code, 404
                )
                with patch(
                    "services.orchestrator.app.GatewayModel.connect", new=AsyncMock()
                ):
                    with patch(
                        "services.orchestrator.app.run",
                        new=AsyncMock(
                            return_value=Result(
                                text="answer", state="done", cost=Decimal("0.01")
                            )
                        ),
                    ):
                        response = client.post(
                            "/query", json={"question": "hello", "lang": "en"}
                        )
                        self.assertEqual(response.json()["state"], "done")
