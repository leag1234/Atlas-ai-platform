"""The document-only completion transport remains recordable and replayable."""

import base64
from copy import deepcopy
import unittest
from unittest.mock import patch

from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.document_chat import process_documents
from services.orchestrator.documents import Attachment
from services.orchestrator.interactions import Interaction
from services.orchestrator.model import GatewayModel


CONFIG: dict[str, object] = {
    "input_eur_per_mtok": "0.0",
    "output_eur_per_mtok": "0.0",
    "max_tokens": 2048,
    "gateway_reserves_quality": True,
}
RESPONSE: dict[str, object] = {
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    "cost_eur": "0.0",
    "provider_model": "recorded-document-provider",
    "observation": {"provider": "escalade", "route": "complexe"},
    "text": "The attachment says the launch is postponed.",
    "calls": [],
}


def request(question: str) -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role="user", content=question)],
        documents=[
            Attachment(
                filename="decision.txt",
                file_data=base64.b64encode(b"The launch is postponed.").decode(),
            )
        ],
    )


def without_timeout(payload: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key != "timeout"}


class DocumentCompletionRecordingTests(unittest.IsolatedAsyncioTestCase):
    async def test_document_completion_is_recorded_replayed_and_exact(self) -> None:
        rows: list[dict[str, object]] = []

        async def provider(
            url: str, payload: dict[str, object], timeout: float
        ) -> dict[str, object]:
            del payload, timeout
            if url.endswith("/agent/config"):
                return deepcopy(CONFIG)
            self.assertTrue(url.endswith("/agent/complete"))
            return deepcopy(RESPONSE)

        async def recording_post(
            url: str, payload: dict[str, object], timeout: float
        ) -> dict[str, object]:
            response = await provider(url, payload, timeout)
            if url.endswith("/agent/complete"):
                rows.append(
                    {
                        "kind": "complete",
                        "request": without_timeout(payload),
                        "response": deepcopy(response),
                    }
                )
            return response

        with patch.object(GatewayModel, "post", staticmethod(recording_post)):
            captured = Interaction()
            await process_documents(request("Summarize the attachment."), captured)

        self.assertEqual(captured.reponse, RESPONSE["text"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "complete")
        recorded = rows[0]
        recorded_request = recorded["request"]
        recorded_response = recorded["response"]
        assert isinstance(recorded_request, dict)
        assert isinstance(recorded_response, dict)
        self.assertNotIn("timeout", recorded_request)

        async def replay_post(
            url: str, payload: dict[str, object], timeout: float
        ) -> dict[str, object]:
            if url.endswith("/agent/config"):
                return await provider(url, payload, timeout)
            self.assertTrue(url.endswith("/agent/complete"))
            self.assertEqual(
                without_timeout(payload),
                recorded_request,
                "unrecorded_m24_provider_request",
            )
            return deepcopy(recorded_response)

        with patch.object(GatewayModel, "post", staticmethod(replay_post)):
            replayed = Interaction()
            await process_documents(request("Summarize the attachment."), replayed)
        self.assertEqual(replayed.reponse, RESPONSE["text"])

        with patch.object(GatewayModel, "post", staticmethod(replay_post)):
            with self.assertRaisesRegex(
                AssertionError, "unrecorded_m24_provider_request"
            ):
                await process_documents(request("List every decision."), Interaction())


if __name__ == "__main__":
    unittest.main()
