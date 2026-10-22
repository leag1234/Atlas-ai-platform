import unittest
import json
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from services.orchestrator.app import app
from services.orchestrator.loop import Query


class InputFilterTests(unittest.TestCase):
    def test_controls_rejected_before_network(self) -> None:
        with (
            TestClient(app) as client,
            patch(
                "services.orchestrator.app.GatewayModel.connect",
                new=AsyncMock(side_effect=RuntimeError),
            ) as connect,
        ):
            for text in ("abc\x00", "\x1b[31m", "abc\u202e", "abc\ud800", "a\x85"):
                self.assertEqual(
                    client.post(
                        "/query",
                        content=json.dumps({"question": text, "lang": "fr"}),
                        headers={"Content-Type": "application/json"},
                    ).status_code,
                    422,
                )
            connect.assert_not_called()

    def test_multilingual_text_preserved(self) -> None:
        text = "Été Grüße español italiano 中文 العربية 👩‍💻\n\tfin"
        self.assertEqual(Query(question=text, lang="fr").question, text)
