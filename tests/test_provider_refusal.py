"""Provider refusals carry a class and identifiers in logs; credit is never an outage."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from decimal import Decimal
import json
import os
from pathlib import Path
from threading import Thread
from typing import Any
import unittest
from unittest.mock import Mock, patch

import dev_gateway as gateway
from provider_refusal import CLASSES, Refusal, classify_refusal, error_envelope
from provider_retry import ProviderResponseError, TransientProviderError

KEY = "fictional-key-value-0001"
PRIVATE = "Fictional private provider text"
# Shapes: a flat error object and a nested one. Identifiers are examples.
FLAT_CAPACITY = {
    "object": "error",
    "message": PRIVATE,
    "type": "service_tier_capacity_exceeded",
    "param": None,
    "code": "3505",
}
NESTED_CREDIT = {
    "error": {
        "message": PRIVATE,
        "type": "insufficient_quota",
        "param": None,
        "code": "insufficient_quota",
    }
}


class Content:
    """An error body that, like a real response, can be read only once."""

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


class Session:
    def __init__(self, status: int, body: object) -> None:
        self.response = Response(
            status, body if isinstance(body, bytes) else json.dumps(body).encode()
        )

    async def __aenter__(self) -> "Session":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def post(self, url: str, **kwargs: object) -> Response:
        return self.response


def plan(fallback: bool = True) -> gateway.Plan:
    body = json.dumps({"messages": [{"role": "user", "content": "private-question"}]})
    other = gateway.Plan(
        body=body,
        endpoint="https://provider.invalid/v1",
        reserved=0,
        input_rate=Decimal("1"),
        output_rate=Decimal("2"),
        provider="fallback",
        model="fallback-model",
        api_key=KEY,
        attempt_bound=1000,
    )
    return gateway.Plan(
        body=body,
        endpoint="https://provider.invalid/v1",
        reserved=0,
        input_rate=Decimal("1"),
        output_rate=Decimal("2"),
        provider="primary",
        model="primary-model",
        api_key=KEY,
        attempt_bound=5000,
        fallback=other if fallback else None,
    )


class ClassTests(unittest.TestCase):
    def test_documented_statuses_and_identifiers_give_one_class(self) -> None:
        cases: list[tuple[int | None, dict[str, Any] | None, str, str, str]] = [
            (402, None, "credit", "", ""),
            (423, None, "credit", "", ""),
            (429, None, "rate_limit", "", ""),
            (503, None, "capacity", "", ""),
            (401, None, "auth", "", ""),
            (403, None, "auth", "", ""),
            (400, None, "other", "", ""),
            (500, None, "other", "", ""),
            (None, None, "other", "", ""),
            (429, FLAT_CAPACITY, "capacity", "service_tier_capacity_exceeded", "3505"),
            (
                429,
                {
                    "object": "error",
                    "message": PRIVATE,
                    "type": "backend_out_of_capacity",
                },
                "capacity",
                "backend_out_of_capacity",
                "",
            ),
            (429, NESTED_CREDIT, "credit", "insufficient_quota", "insufficient_quota"),
            (403, NESTED_CREDIT, "credit", "insufficient_quota", "insufficient_quota"),
            (
                429,
                {"error": {"type": "rate_limit_error", "code": 429}},
                "rate_limit",
                "rate_limit_error",
                "429",
            ),
            (
                503,
                {"error": {"type": "TooManyRequests"}},
                "rate_limit",
                "toomanyrequests",
                "",
            ),
            (
                400,
                {"detail": {"type": "PermissionDenied", "code": "bad-shape.v2"}},
                "auth",
                "permissiondenied",
                "bad_shape_v2",
            ),
            (
                402,
                {"error": {"type": "rate_limit_error"}},
                "credit",
                "rate_limit_error",
                "",
            ),
            (
                500,
                {"error": {"type": "load_balancer_error", "code": "internal"}},
                "other",
                "load_balancer_error",
                "internal",
            ),
        ]
        for status, envelope, kind, error_type, error_code in cases:
            with self.subTest(status=status, envelope=envelope):
                refusal = classify_refusal(status, envelope, (KEY,))
                self.assertIn(refusal.kind, CLASSES)
                self.assertEqual(
                    (refusal.kind, refusal.error_type, refusal.error_code),
                    (kind, error_type, error_code),
                )

    def test_identifiers_are_sanitised_or_dropped(self) -> None:
        dropped: list[object] = [
            "two words",
            "x" * 61,
            "",
            True,
            -1,
            10**9,
            3.5,
            ["rate_limit"],
            "https://provider.invalid/x",
            KEY,
            "prefix-" + KEY.upper(),
            KEY[4:16],
        ]
        for value in dropped:
            with self.subTest(value=value):
                refusal = classify_refusal(
                    429, {"error": {"type": value, "code": value}}, (KEY,)
                )
                self.assertEqual((refusal.error_type, refusal.error_code), ("", ""))
                self.assertEqual(refusal.kind, "rate_limit")
        kept = classify_refusal(429, {"type": "Rate-Limit.Error_2", "code": 1300})
        self.assertEqual(kept.error_type, "rate_limit_error_2")
        self.assertEqual(kept.error_code, "1300")
        self.assertRegex(kept.error_type, r"^[a-z0-9_]{1,60}$")

    def test_log_fields_never_contain_the_message(self) -> None:
        refusal = classify_refusal(429, FLAT_CAPACITY, (KEY,))
        self.assertEqual(
            refusal.fields(),
            "class=capacity type=service_tier_capacity_exceeded code=3505",
        )
        self.assertEqual(Refusal().fields(), "class=other type=- code=-")
        self.assertNotIn(PRIVATE, refusal.fields())


class EnvelopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unusable_bodies_give_no_envelope(self) -> None:
        bodies = [b"<html>" + PRIVATE.encode() + b"</html>", b"[1]", b"", b"x" * 20000]
        for body in bodies:
            with self.subTest(size=len(body)):
                response: Any = Response(429, body)
                self.assertIsNone(await error_envelope(response))

    async def test_failing_body_gives_no_envelope(self) -> None:
        class Failing(Content):
            async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
                raise TimeoutError(PRIVATE)
                yield b""

        response: Any = Response(429, b"")
        response.content = Failing(b"")
        self.assertIsNone(await error_envelope(response))


class DevTransportTests(unittest.IsolatedAsyncioTestCase):
    async def refused(
        self, status: int, body: object, fallback: bool = True
    ) -> tuple[BaseException, list[str]]:
        session = Session(status, body)
        with (
            patch("dev_gateway.aiohttp.ClientSession", return_value=session),
            self.assertLogs("dev_gateway", "WARNING") as logs,
            self.assertRaises(RuntimeError) as caught,
        ):
            _ = [piece async for piece in gateway.transport(plan(fallback))]
        for forbidden in (PRIVATE, KEY, "private-question"):
            self.assertNotIn(forbidden, str(logs.output))
        return caught.exception, [record.getMessage() for record in logs.records]

    async def test_capacity_429_is_logged_with_class_and_identifiers(self) -> None:
        error, lines = await self.refused(429, FLAT_CAPACITY)
        assert isinstance(error, TransientProviderError)
        self.assertEqual(str(error), "provider_error")
        self.assertEqual((error.status, error.detail), (429, "http_429"))
        assert error.refusal is not None
        self.assertEqual(error.refusal.kind, "capacity")
        self.assertEqual(
            lines,
            [
                "devapi_provider_refusal provider=primary model=primary-model "
                "status=429 class=capacity type=service_tier_capacity_exceeded "
                "code=3505"
            ],
        )

    async def test_unreadable_429_keeps_the_documented_status_class(self) -> None:
        error, lines = await self.refused(429, b"<html>" + PRIVATE.encode())
        assert isinstance(error, TransientProviderError)
        self.assertEqual(str(error), "provider_error")
        self.assertEqual(
            lines,
            [
                "devapi_provider_refusal provider=primary model=primary-model "
                "status=429 class=rate_limit type=- code=-"
            ],
        )

    async def test_credit_refusal_on_any_status_is_a_quota_refusal(self) -> None:
        cases: list[tuple[int, object]] = [
            (402, b""),
            (423, b""),
            (429, NESTED_CREDIT),
            (403, NESTED_CREDIT),
            (400, {"object": "error", "type": "billing_error", "message": PRIVATE}),
        ]
        for status, body in cases:
            with self.subTest(status=status):
                error, lines = await self.refused(status, body)
                assert isinstance(error, TransientProviderError)
                self.assertEqual(str(error), "provider_quota")
                self.assertEqual(error.status, status)
                self.assertEqual(error.detail, f"http_{status}")
                self.assertFalse(error.started)
                self.assertIn(f"status={status} class=credit ", lines[0])
                self.assertEqual(lines[1], "devapi_provider_credit")

    async def test_other_refusals_keep_their_behaviour_and_are_logged(self) -> None:
        cases = [(401, "auth", False), (404, "other", False), (503, "capacity", True)]
        for status, kind, transient in cases:
            with self.subTest(status=status):
                error, lines = await self.refused(status, {"error": {"message": "x"}})
                self.assertEqual(str(error), "provider_error")
                self.assertEqual(isinstance(error, TransientProviderError), transient)
                self.assertEqual(
                    lines,
                    [
                        "devapi_provider_refusal provider=primary model=primary-model "
                        f"status={status} class={kind} type=- code=-"
                    ],
                )


class FallbackLogTests(unittest.IsolatedAsyncioTestCase):
    async def fallback_line(self, error: TransientProviderError) -> str:
        raw: str = json.loads(
            (Path(__file__).parent / "fixtures/devapi-streams.json").read_text()
        )["auto"]

        async def transport(current: gateway.Plan) -> AsyncGenerator[bytes, None]:
            if current.fallback is not None:
                raise error
            yield raw.encode()

        with (
            patch.object(gateway, "transport", transport),
            self.assertLogs("dev_gateway", "WARNING") as logs,
        ):
            frames = [frame async for frame in gateway.events(plan())]
        self.assertIn("cost_micro_eur", frames[-1])
        lines = [record.getMessage() for record in logs.records]
        self.assertEqual(len(lines), 1)
        return lines[0]

    async def test_fallback_line_carries_class_and_identifiers(self) -> None:
        line = await self.fallback_line(
            TransientProviderError(
                "provider_error",
                detail="http_429",
                status=429,
                refusal=classify_refusal(429, FLAT_CAPACITY),
            )
        )
        self.assertEqual(
            line,
            "devapi_transient_fallback from=primary/primary-model "
            "to=fallback/fallback-model reason=provider_error http_429 "
            "started=False class=capacity type=service_tier_capacity_exceeded "
            "code=3505",
        )

    async def test_fallback_without_provider_answer_is_class_other(self) -> None:
        line = await self.fallback_line(
            TransientProviderError(
                "provider_timeout", started=True, detail="TimeoutError", status=504
            )
        )
        self.assertTrue(
            line.endswith(
                "reason=provider_timeout TimeoutError started=True "
                "class=other type=- code=-"
            ),
            line,
        )


class DevCallerTests(unittest.TestCase):
    def test_credit_refusal_reaches_the_caller_as_quota_with_real_status(self) -> None:
        from services.orchestrator.dev_inference import (
            _provider_error_status,
            provider_failure,
        )

        for status in (402, 429, 403, 423):
            with self.subTest(status=status):
                failure = provider_failure(
                    TransientProviderError(
                        "provider_quota", detail=f"http_{status}", status=status
                    )
                )["error"]
                self.assertEqual(failure["type"], "provider_quota")
                self.assertEqual(failure["provider_status"], status)
                self.assertIn("account credit exhausted", failure["message"])
                self.assertEqual(_provider_error_status(failure), 402)
        unknown = provider_failure(TransientProviderError("provider_quota"))["error"]
        self.assertEqual(unknown["provider_status"], 402)

    def test_rate_limit_and_outage_keep_their_public_shape(self) -> None:
        from services.orchestrator.dev_inference import provider_failure

        limited = provider_failure(
            TransientProviderError(
                "provider_error", detail="http_429", status=429, retry_after="7"
            )
        )["error"]
        self.assertEqual(
            (limited["type"], limited["provider_status"], limited["retry_after"]),
            ("rate_limit", 429, "7"),
        )
        down = provider_failure(
            TransientProviderError("provider_error", detail="http_503", status=503)
        )["error"]
        self.assertEqual(down["type"], "provider_unavailable")


class ChatTransportTests(unittest.IsolatedAsyncioTestCase):
    async def diagnostic(
        self, status: int, body: object
    ) -> tuple[BaseException, dict[str, Any]]:
        from agent_provider import AgentRequest
        from serverless import ServerlessPolicy
        from serverless_support import environment
        from stream_transport import attempt

        session = Session(status, body)
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.aiohttp.ClientSession", return_value=session),
            self.assertLogs("stream_transport", level="WARNING") as logs,
            self.assertRaises(RuntimeError) as caught,
        ):
            _ = [
                event
                async for event in attempt(
                    AgentRequest(
                        messages=[{"role": "user", "content": "private-question"}],
                        tools=[],
                        timeout=5.0,
                    ),
                    ServerlessPolicy().models["text"],
                    5.0,
                )
            ]
        self.assertNotIn("private-question", str(logs.output))
        logged: dict[str, Any] = json.loads(logs.records[0].getMessage())
        return caught.exception, logged

    async def test_credit_refusal_is_classified_from_a_body_read_once(self) -> None:
        error, logged = await self.diagnostic(429, NESTED_CREDIT)
        assert isinstance(error, ProviderResponseError)
        self.assertEqual((error.status, error.refusal_class), (429, "credit"))
        self.assertEqual(logged["refusal_class"], "credit")
        self.assertEqual(logged["refusal_type"], "insufficient_quota")
        self.assertEqual(logged["refusal_code"], "insufficient_quota")
        # The request-shape details come from the same single read.
        self.assertEqual(logged["provider_error_type"], "insufficient_quota")

    async def test_flat_error_object_and_plain_statuses(self) -> None:
        error, logged = await self.diagnostic(429, FLAT_CAPACITY)
        assert isinstance(error, ProviderResponseError)
        self.assertEqual(error.refusal_class, "capacity")
        self.assertEqual(logged["refusal_class"], "capacity")
        self.assertEqual(logged["refusal_type"], "service_tier_capacity_exceeded")
        self.assertEqual(logged["refusal_code"], "3505")
        error, logged = await self.diagnostic(402, b"")
        assert isinstance(error, ProviderResponseError)
        self.assertEqual(error.refusal_class, "credit")
        self.assertEqual(logged["refusal_class"], "credit")
        self.assertNotIn("refusal_type", logged)
        error, logged = await self.diagnostic(503, b"<html></html>")
        assert isinstance(error, TransientProviderError)
        self.assertEqual(logged["refusal_class"], "capacity")


class ChatBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """Real gateway HTTP boundary, then the orchestrator's gateway client."""

    async def asyncSetUp(self) -> None:
        from http_gateway import serve

        self.server = serve(Mock(), 0)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    async def asyncTearDown(self) -> None:
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        await asyncio.to_thread(self.thread.join, 2)

    async def stream_failure(self, error: BaseException) -> BaseException:
        from services.orchestrator.stream_client import receive
        from stream_transport import StreamRequest

        async def refused(payload: object) -> AsyncGenerator[dict[str, object], None]:
            raise error
            yield {}

        async def sink(event: dict[str, object]) -> None:
            return None

        payload = StreamRequest(
            messages=[{"role": "user", "content": "Hello"}], tools=[], timeout=5.0
        ).model_dump()
        with patch("http_gateway.AgentProvider") as provider:
            provider.return_value.stream = refused
            with self.assertRaises(Exception) as caught:
                await receive(self.base + "/agent/stream", payload, 5, sink)
        return caught.exception

    async def test_streamed_credit_refusal_is_not_a_provider_outage(self) -> None:
        from services.orchestrator.model import GatewayError

        error = await self.stream_failure(
            ProviderResponseError(429, classify_refusal(429, NESTED_CREDIT))
        )
        assert isinstance(error, GatewayError)
        self.assertEqual((error.code, error.status), ("provider_quota", 402))
        self.assertEqual(
            error.detail,
            "The model provider refused the request: account credit exhausted "
            "(provider status 429).",
        )
        self.assertNotIn(PRIVATE, error.detail)

    async def test_streamed_outage_keeps_its_previous_shape(self) -> None:
        from services.orchestrator.model import GatewayError

        for status in (429, 500):
            with self.subTest(status=status):
                error = await self.stream_failure(
                    ProviderResponseError(status, classify_refusal(status, None))
                )
                self.assertNotIsInstance(error, GatewayError)
                self.assertEqual(str(error), "gateway_stream_error")

    async def test_json_credit_refusal_is_not_a_provider_outage(self) -> None:
        from services.orchestrator.model import GatewayError, GatewayModel

        async def refused(*args: object) -> object:
            raise ProviderResponseError(402, classify_refusal(402, None))

        with patch("http_gateway.AgentProvider") as provider:
            provider.return_value.complete = refused
            with self.assertRaises(GatewayError) as caught:
                await GatewayModel.post(self.base + "/agent/complete", {}, 5)
        self.assertEqual(
            (caught.exception.code, caught.exception.status), ("provider_quota", 402)
        )
        self.assertIn("provider status 402", caught.exception.detail)


class ChatCallerTests(unittest.TestCase):
    def test_chat_reports_credit_refusal_as_quota_in_json_and_stream(self) -> None:
        import tempfile

        from fastapi.testclient import TestClient
        from services.orchestrator.chat_api import app
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.model import GatewayError
        from services.orchestrator.stream_client import provider_credit_detail

        async def reject(request: ChatRequest, item: Interaction) -> None:
            raise GatewayError("provider_quota", 402, provider_credit_detail(402))

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            TestClient(app) as client,
            patch("services.orchestrator.chat_api.process", side_effect=reject),
        ):
            for streaming in (False, True):
                with self.subTest(streaming=streaming):
                    reply = client.post(
                        "/v1/chat/completions",
                        json={
                            "ui_locale": "fr",
                            "lang": "fr",
                            "stream": streaming,
                            "messages": [{"role": "user", "content": "Hello"}],
                        },
                    )
                    self.assertIn("provider_quota", reply.text)
                    self.assertIn(
                        "account credit exhausted (provider status 402)", reply.text
                    )
                    self.assertNotIn("provider_error", reply.text)
                    if not streaming:
                        self.assertEqual(reply.status_code, 402)
