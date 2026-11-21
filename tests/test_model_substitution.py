"""C15: a request for model X is served by X, or the response names who answered.

A fictional provider account refuses the primary target with HTTP 402 (credit
exhausted); the configured fallback answers. Every format, streamed or not, must
then carry the served model in ``model``, the requested model, the served model
and the reason in a header and in the ``atlas`` field, and the fallback's price.
"""

from collections.abc import AsyncGenerator, AsyncIterator
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import dev_gateway as gateway
from provider_refusal import classify_refusal
from provider_retry import TransientProviderError

PRIMARY = "mistral/zai-glm-5-3"
FALLBACK = "scaleway/deepseek-v4-flash-0731"
# 1000 prompt and 500 completion tokens at the fallback's price (0.40 and 0.80
# EUR per million): 400 + 400 micro-euros. At the primary's (1.19 and 3.74) the
# same call would cost 1190 + 1870.
FALLBACK_COST = 800


class Content:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        body, self.body = self.body, b""
        for offset in range(0, len(body), size):
            yield body[offset : offset + size]


class Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.content = Content(body)
        self.headers: dict[str, str] = {}

    async def __aenter__(self) -> "Response":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def answer(text: str = '{"answer": "Fictional reply."}') -> bytes:
    frames = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": text},
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 500}},
    ]
    return (
        b"".join(b"data: " + json.dumps(f).encode() + b"\n\n" for f in frames)
        + b"data: [DONE]\n\n"
    )


class Provider:
    """Fake providers: the Mistral account is out of credit when ``broke``."""

    def __init__(self) -> None:
        self.broke = True
        self.models: list[str] = []

    def __call__(self, *args: object, **kwargs: object) -> "Provider":
        return self

    async def __aenter__(self) -> "Provider":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> Response:
        model = json.loads(kwargs["data"])["model"]
        self.models.append(model)
        if self.broke and model.startswith("zai-glm"):
            return Response(
                402,
                json.dumps(
                    {"object": "error", "message": "x", "type": "billing_error"}
                ).encode(),
            )
        return Response(200, answer())


BODIES: dict[str, dict[str, Any]] = {
    "chat/completions": {
        "model": "atlas-code",
        "reasoning_effort": "high",
        "messages": [{"role": "user", "content": "Fictional question"}],
    },
    "responses": {
        "model": "atlas-code",
        "reasoning": {"effort": "high"},
        "input": "Fictional question",
        "store": False,
    },
    "messages": {
        "model": "atlas-code",
        "max_tokens": 1024,
        "output_config": {"effort": "high"},
        "messages": [{"role": "user", "content": "Fictional question"}],
    },
}


class SubstitutionTests(unittest.TestCase):
    def setUp(self) -> None:
        from services.orchestrator.api_keys import Store
        from services.orchestrator.chat_api import app

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(root / "keys.sqlite", "fictional-pepper-" * 4)
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ATLAS_DATA_DIR": str(root),
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_PROJECT_DSN": "",
                    "ATLAS_RAW_CALL_MAX_MICRO_EUR": "5000000",
                    "ATLAS_USER_DAILY_EUR": "10",
                    "ATLAS_GLOBAL_DAILY_EUR": "20",
                    "ATLAS_PROVIDER": "",
                    "SCW_GENERATIVE_BASE_URL": "https://provider.invalid/v1",
                    "MISTRAL_API_KEY": "fictional-key",
                    "SCW_GENERATIVE_API_KEY": "fictional-key",
                },
            )
        )
        self.enterContext(
            patch(
                "services.orchestrator.api_key_edge.configured_store",
                return_value=self.store,
            )
        )
        self.provider = Provider()
        self.enterContext(patch("dev_gateway.aiohttp.ClientSession", self.provider))
        self.client = self.enterContext(TestClient(app))
        self.row, secret = self.store.create(
            org_id="fictional-org",
            member_id="fictional-member",
            subject="tester@example.invalid",
            budget_user="fictional-user",
            name="coding",
            scopes=["model"],
            created_by="operator",
            daily_requests=100,
            daily_micro_eur=1000000,
        )
        self.headers = {"Authorization": "Bearer " + secret}

    def charged(self) -> int:
        usage: dict[str, Any] = self.store.usage(self.row["id"])
        return int(usage["charged_micro_eur"])

    def call(self, path: str, body: dict[str, Any], stream: bool) -> Any:
        reply = self.client.post(
            "/api/atlas/v1/" + path,
            json=body | {"stream": stream},
            headers=self.headers,
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        return reply

    @staticmethod
    def events(reply: Any) -> list[dict[str, Any]]:
        return [
            json.loads(line[6:])
            for line in reply.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]

    def final(self, path: str, reply: Any, stream: bool) -> dict[str, Any]:
        """The response object (JSON) or the last stream frame naming the answer."""
        if not stream:
            return dict(reply.json())
        events = self.events(reply)
        if path == "responses":
            self.assertEqual(events[-1]["type"], "response.completed")
            return dict(events[-1]["response"])
        if path == "messages":
            self.assertEqual(events[-1]["type"], "message_stop")
            return {"model": None, **events[-1]}
        return dict(events[-1])

    def test_every_format_names_the_served_model_reason_and_price(self) -> None:
        substitution = {
            "from": PRIMARY,
            "reason": "provider_quota",
            "provider_status": 402,
        }
        for path, body in BODIES.items():
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    before = self.charged()
                    reply = self.call(path, body, stream)
                    result = self.final(path, reply, stream)
                    atlas = result["atlas"]
                    self.assertEqual(
                        atlas,
                        {
                            "requested_model": "atlas-code",
                            "served_model": FALLBACK,
                            "substitution": substitution,
                            "cost_micro_eur": FALLBACK_COST,
                        },
                    )
                    self.assertEqual(result["served_by"], FALLBACK)
                    self.assertEqual(
                        reply.headers["x-atlas-requested-model"], "atlas-code"
                    )
                    if stream:
                        # Sent before any provider answered: never a served name.
                        self.assertNotIn("x-atlas-served-by", reply.headers)
                        self.assertEqual(
                            reply.headers["x-atlas-planned-target"], PRIMARY
                        )
                    else:
                        self.assertEqual(result["model"], FALLBACK)
                        self.assertEqual(reply.headers["x-atlas-served-by"], FALLBACK)
                        self.assertEqual(
                            reply.headers["x-atlas-substitution"],
                            f"reason=provider_quota; from={PRIMARY}; provider_status=402",
                        )
                        self.assertEqual(
                            reply.headers["x-atlas-cost-micro-eur"], str(FALLBACK_COST)
                        )
                    # The ledger charges the served model's price, as reported.
                    after = self.charged()
                    self.assertEqual(after - before, FALLBACK_COST)

    def test_stream_frames_carry_the_served_model(self) -> None:
        chunks = self.events(
            self.call("chat/completions", BODIES["chat/completions"], True)
        )
        self.assertEqual({chunk["model"] for chunk in chunks}, {FALLBACK})
        events = self.events(self.call("responses", BODIES["responses"], True))
        # response.created leaves before the provider call; the end names the answer.
        self.assertEqual(events[0]["response"]["model"], "atlas-code")
        self.assertNotIn("atlas", events[0]["response"])
        self.assertEqual(events[-1]["response"]["model"], FALLBACK)

    def test_answer_of_the_requested_model_keeps_its_name(self) -> None:
        self.provider.broke = False
        for path, body in BODIES.items():
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    reply = self.call(path, body, stream)
                    result = self.final(path, reply, stream)
                    self.assertEqual(result["served_by"], PRIMARY)
                    self.assertIsNone(result["atlas"]["substitution"])
                    self.assertEqual(result["atlas"]["served_model"], PRIMARY)
                    # 1000 x 1.19 + 500 x 3.74 micro-euros per million tokens.
                    self.assertEqual(result["atlas"]["cost_micro_eur"], 3060)
                    self.assertNotIn("x-atlas-substitution", reply.headers)
                    if not stream:
                        self.assertEqual(result["model"], "atlas-code")

    def test_validated_json_answer_names_the_substitution(self) -> None:
        body = BODIES["chat/completions"] | {"response_format": {"type": "json_object"}}
        for stream in (False, True):
            with self.subTest(stream=stream):
                reply = self.call("chat/completions", body, stream)
                result = self.final("chat/completions", reply, stream)
                self.assertEqual(result["model"], FALLBACK)
                self.assertEqual(
                    result["atlas"]["substitution"]["reason"], "provider_quota"
                )
                self.assertEqual(result["atlas"]["cost_micro_eur"], FALLBACK_COST)

    def test_pinned_request_is_refused_never_substituted(self) -> None:
        reply = self.client.post(
            "/api/atlas/v1/chat/completions",
            json=BODIES["chat/completions"] | {"model": PRIMARY},
            headers=self.headers,
        )
        self.assertEqual(reply.status_code, 402, reply.text)
        self.assertEqual(reply.json()["error"]["type"], "provider_quota")
        self.assertNotIn("deepseek-v4-flash-0731", self.provider.models)


def plan() -> gateway.Plan:
    body = json.dumps({"messages": [], "max_tokens": 64})
    fallback = gateway.Plan(
        body,
        "https://fallback.invalid",
        0,
        Decimal(0),
        Decimal(0),
        provider="fallback",
        model="fallback-model",
    )
    return gateway.Plan(
        body,
        "https://primary.invalid",
        0,
        Decimal(0),
        Decimal(0),
        provider="primary",
        model="primary-model",
        fallback=fallback,
    )


class GatewaySubstitutionTests(unittest.IsolatedAsyncioTestCase):
    async def frames(
        self, error: Exception | None, primary: bytes
    ) -> list[dict[str, Any]]:
        async def transport(current: gateway.Plan) -> AsyncGenerator[bytes, None]:
            if current.fallback is not None:
                if error is not None:
                    raise error
                yield primary
                return
            yield answer("Fallback reply.")

        with patch.object(gateway, "transport", transport):
            return [frame async for frame in gateway.events(plan())]

    async def test_every_fallback_frame_names_target_and_reason(self) -> None:
        cases = [
            (
                TransientProviderError("provider_quota", status=402),
                "provider_quota",
                402,
            ),
            (
                TransientProviderError(
                    "provider_error",
                    status=429,
                    refusal=classify_refusal(
                        429, {"type": "service_tier_capacity_exceeded"}
                    ),
                ),
                "capacity",
                429,
            ),
            (TransientProviderError("provider_error", status=429), "rate_limit", 429),
            (
                TransientProviderError("provider_timeout", status=504),
                "provider_timeout",
                504,
            ),
            (
                TransientProviderError("provider_error", status=503),
                "provider_unavailable",
                503,
            ),
        ]
        for error, reason, status in cases:
            with self.subTest(reason=reason):
                frames = await self.frames(error, b"")
                self.assertTrue(frames)
                for frame in frames:
                    self.assertEqual(
                        frame["substitution"],
                        {
                            "from": "primary/primary-model",
                            "served": "fallback/fallback-model",
                            "reason": reason,
                            "provider_status": status,
                        },
                    )
                self.assertEqual(frames[-1]["served_by"], "fallback/fallback-model")

    async def test_reasoning_only_answers_name_their_substitution(self) -> None:
        reasoning_only = (
            b'data: {"choices":[{"index":0,"delta":{"reasoning_content":"Hmm"},'
            b'"finish_reason":"stop"}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":4}}\n\n'
            b"data: [DONE]\n\n"
        )
        frames = await self.frames(None, reasoning_only)
        self.assertEqual(frames[-1]["substitution"]["reason"], "reasoning_only_answer")
        self.assertEqual(frames[-1]["served_by"], "fallback/fallback-model")

    async def test_primary_answer_carries_no_substitution(self) -> None:
        async def transport(current: gateway.Plan) -> AsyncGenerator[bytes, None]:
            yield answer("Primary reply.")

        with patch.object(gateway, "transport", transport):
            frames = [frame async for frame in gateway.events(plan())]
        self.assertTrue(all("substitution" not in frame for frame in frames))


class InterfaceNoticeTests(unittest.TestCase):
    """The chat interface names the model that answered after a provider fallback."""

    def turn(
        self, fallback: bool, streaming: bool
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        from services.orchestrator.chat_api import app
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.stream_client import sink_context

        async def respond(request: ChatRequest, item: Interaction) -> None:
            sink = sink_context.get()
            if sink is not None and fallback:
                await sink({"phase": "provider_fallback"})
            item.reponse = "Fictional answer"
            if sink is not None:
                await sink({"delta": {"content": item.reponse}})
            item.provider_model = "fictional-fallback-model"
            item.state = "done"

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            TestClient(app) as client,
            patch("services.orchestrator.chat_api.process", side_effect=respond),
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "atlas-glm",
                    "stream": streaming,
                    "ui_locale": "fr",
                    "messages": [{"role": "user", "content": "Fictional question"}],
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        if not streaming:
            return response.json()["atlas"], []
        rows = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        statuses = [row["event"]["data"] for row in rows if "event" in row]
        return rows[-1]["atlas"], statuses

    def test_fallback_turn_shows_a_status_line_naming_the_model(self) -> None:
        metadata, statuses = self.turn(True, True)
        notice = "Réponse de fictional-fallback-model : le fournisseur du modèle choisi a échoué"
        self.assertTrue(metadata["provider_fallback"])
        self.assertEqual(metadata["status"], notice)
        self.assertEqual(statuses[-1]["description"], notice)
        self.assertTrue(statuses[-1]["done"])
        self.assertFalse(statuses[-1]["hidden"])
        metadata, _ = self.turn(True, False)
        self.assertEqual(metadata["status"], notice)

    def test_turn_of_the_selected_model_has_no_notice(self) -> None:
        metadata, statuses = self.turn(False, True)
        self.assertFalse(metadata["provider_fallback"])
        self.assertEqual(metadata["status"], "")
        self.assertNotIn("fictional-fallback-model", json.dumps(statuses))


if __name__ == "__main__":
    unittest.main()
