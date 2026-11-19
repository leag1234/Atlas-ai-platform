"""A refused or failed assistant turn pays its measured calls, never the reservation.

Staging 2026-11-19: with web search down, assistant turns refused with 503
quota_exceeded were each charged the 0.10 EUR answer reservation although only the
routing call had run. Every refusal outcome on every API route (Chat Completions,
Responses, Messages; streamed or not) settles the key and the daily ledger at the
cost the provider calls measured. A call cut in flight still keeps the reservation.
"""

import json
import sqlite3
import unittest
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any, cast
from unittest.mock import patch

from fastapi.testclient import TestClient

from services.orchestrator.api_keys import Store
from services.orchestrator.chat_api import app
from services.orchestrator.daily_budget import DailyLedger
import test_api_keys

QUOTA_DETAIL = (
    "Web search unavailable: quota exhausted. Provider HTTP 429. "
    "No verified result was obtained (required: at least 1)."
)


class FakeContent:
    def __init__(self, body: bytes, lines: list[bytes]) -> None:
        self.body, self.lines = body, lines

    async def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        yield self.body

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self.each()

    async def each(self) -> AsyncIterator[bytes]:
        for line in self.lines:
            yield line


class FakeResponse:
    def __init__(
        self, status: int, body: object = None, lines: list[object] | None = None
    ) -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self.content = FakeContent(
            json.dumps(body).encode() if body is not None else b"",
            [b"data: " + json.dumps(line).encode() + b"\n" for line in lines or []],
        )

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


@contextmanager
def gateway(*answers: FakeResponse) -> Iterator[None]:
    """The model gateway answers these responses, in order."""
    queue = list(answers)

    def post(self: Any, url: str, **kwargs: Any) -> FakeResponse:
        return queue.pop(0)

    with patch("aiohttp.ClientSession.post", new=post):
        yield


async def routed(cost: str = "0.0009") -> None:
    """One measured gateway call (the routing call of the staging turns)."""
    from services.orchestrator.model import GatewayModel

    with gateway(FakeResponse(200, {"cost_eur": cost})):
        await GatewayModel.post("http://gateway.invalid/agent/complete", {}, 1)


async def quiet(event: dict[str, object]) -> None:
    return None


def routes(stream: bool) -> list[tuple[str, dict[str, Any]]]:
    question = [{"role": "user", "content": "What is the news today?"}]
    return [
        (
            "/api/atlas/v1/chat/completions",
            {"model": "atlas", "stream": stream, "messages": question},
        ),
        (
            "/api/atlas/v1/responses",
            {"model": "atlas", "stream": stream, "input": question[0]["content"]},
        ),
        (
            "/api/atlas/v1/messages",
            {
                "model": "atlas",
                "stream": stream,
                "max_tokens": 256,
                "messages": question,
            },
        ),
    ]


class RefusedTurnBillingTests(unittest.TestCase):
    store: Store

    def setUp(self) -> None:
        # The key edge fixture of the key tests (temporary key store and ledgers).
        test_api_keys.KeyTests.setUp(cast(Any, self))

    def create(self, **overrides: Any) -> tuple[dict[str, Any], str]:
        return test_api_keys.KeyTests.create(cast(Any, self), **overrides)

    def settle(
        self, process: Any, path: str, body: dict[str, Any]
    ) -> tuple[sqlite3.Row, Any, int]:
        """One assistant request: its key row, its reply and the day's ledger."""
        created, key = self.create()
        before = self.ledger_day()
        with (
            TestClient(app) as client,
            patch("services.orchestrator.chat_api.process", process),
        ):
            reply = client.post(
                path, headers={"Authorization": "Bearer " + key}, json=body
            )
        with sqlite3.connect(self.store.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT charged,state,estimated FROM api_key_requests WHERE key_id=?",
                (created["id"],),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        return rows[0], reply, self.ledger_day() - before

    def ledger_day(self) -> int:
        with sqlite3.connect(DailyLedger().path) as db:
            try:
                return int(
                    db.execute(
                        "SELECT COALESCE(SUM(measured_micro),0) FROM budget_ledger"
                        " WHERE kind='day'"
                    ).fetchone()[0]
                )
            except sqlite3.OperationalError:
                return 0

    def test_search_quota_refusal_pays_only_the_routing_call(self) -> None:
        from services.orchestrator.model import GatewayError

        async def refused(payload: Any, item: Any) -> None:
            await routed()
            # The pipeline holds the answer reservation in cout_eur meanwhile.
            item.cout_eur = 0.10
            raise GatewayError("quota_exceeded", 503, QUOTA_DETAIL)

        for stream in (False, True):
            for path, body in routes(stream):
                with self.subTest(path=path, stream=stream):
                    row, reply, day = self.settle(refused, path, body)
                    self.assertEqual(
                        (row["charged"], row["state"], row["estimated"]),
                        (900, "complete", 0),
                    )
                    self.assertEqual(day, 900)
                    self.assertNotIn('"finish_reason": "stop"', reply.text)
                    if path.endswith("/chat/completions") and not stream:
                        self.assertEqual(reply.status_code, 503, reply.text)
                        error = reply.json()
                        self.assertEqual(error["error"]["code"], "quota_exceeded")
                        self.assertIn("quota exhausted", error["error"]["message"])
                        self.assertEqual(error["atlas"]["cost_eur"], 0.0009)
                        self.assertEqual(error["atlas"]["cost_kind"], "measured")

    def test_gateway_refusals_cost_nothing(self) -> None:
        from services.orchestrator.model import GatewayModel

        refusals = {
            "provider_quota": FakeResponse(
                402, {"code": "provider_quota", "provider_status": 402}
            ),
            "cost_budget": FakeResponse(
                429,
                {
                    "code": "cost_budget",
                    "unit": "microEUR",
                    "measured": 100000,
                    "limit": 0,
                },
            ),
            "context_exceeded": FakeResponse(413, {"tokens": 90000, "limit": 32000}),
        }
        for name, response in refusals.items():
            for stream in (False, True):
                path, body = routes(stream)[0]

                async def refused(
                    payload: Any, item: Any, response: FakeResponse = response
                ) -> None:
                    await routed("0.0012")
                    item.cout_eur = 0.10
                    with gateway(response):
                        await GatewayModel.post(
                            "http://gateway.invalid/agent/complete", {}, 1
                        )

                with self.subTest(refusal=name, stream=stream):
                    row, reply, day = self.settle(refused, path, body)
                    self.assertEqual((row["charged"], row["state"]), (1200, "complete"))
                    self.assertEqual(day, 1200)

    def test_stream_refusal_before_output_costs_nothing(self) -> None:
        from services.orchestrator.stream_client import receive

        async def refused(payload: Any, item: Any) -> None:
            item.cout_eur = 0.10
            with gateway(
                FakeResponse(
                    200, lines=[{"error": "provider_quota", "provider_status": 402}]
                )
            ):
                await receive("http://gateway.invalid/agent/stream", {}, 1, quiet)

        for path, body in routes(True):
            with self.subTest(path=path):
                row, _, day = self.settle(refused, path, body)
                self.assertEqual((row["charged"], row["state"]), (0, "complete"))
                self.assertEqual(day, 0)

    def test_unmeasured_stream_keeps_the_reservation(self) -> None:
        from services.orchestrator.stream_client import receive

        cases: dict[str, list[object]] = {
            # The provider failed: tokens may have been consumed, never measured.
            "provider_failure": [{"error": "provider_failure", "retries": 1}],
            # A refusal after answer text may follow consumed tokens.
            "refusal_after_text": [
                {"delta": {"content": "Partial"}},
                {"error": "provider_quota", "provider_status": 402},
            ],
        }
        for name, lines in cases.items():

            async def failed(
                payload: Any, item: Any, lines: list[object] = lines
            ) -> None:
                await routed()
                with gateway(FakeResponse(200, lines=lines)):
                    await receive("http://gateway.invalid/agent/stream", {}, 1, quiet)

            path, body = routes(False)[0]
            with self.subTest(case=name):
                row, reply, day = self.settle(failed, path, body)
                self.assertEqual((row["charged"], row["state"]), (100000, "unknown"))
                self.assertEqual(day, 100000)
                self.assertEqual(
                    reply.json()["atlas"]["cost_kind"], "reserved_upper_bound"
                )

    def test_delivered_answer_is_charged_its_measured_cost(self) -> None:
        async def answered(payload: Any, item: Any) -> None:
            await routed("0.0123")
            item.state, item.reponse, item.cout_eur = "done", "The answer.", 0.0123

        for stream in (False, True):
            for path, body in routes(stream):
                with self.subTest(path=path, stream=stream):
                    row, reply, day = self.settle(answered, path, body)
                    self.assertEqual(reply.status_code, 200, reply.text)
                    self.assertEqual(
                        (row["charged"], row["state"]), (12300, "complete")
                    )
                    self.assertEqual(day, 12300)
                    if path.endswith("/chat/completions") and not stream:
                        self.assertEqual(reply.json()["atlas"]["cost_kind"], "measured")

    def test_refusal_before_any_call_costs_nothing(self) -> None:
        from services.orchestrator.model import GatewayError

        async def refused(payload: Any, item: Any) -> None:
            raise GatewayError("search_unavailable", 503, "Web search unavailable.")

        for stream in (False, True):
            path, body = routes(stream)[0]
            with self.subTest(stream=stream):
                row, _, day = self.settle(refused, path, body)
                self.assertEqual((row["charged"], row["state"]), (0, "complete"))
                self.assertEqual(day, 0)

    def test_untracked_call_keeps_the_turns_reserved_cost(self) -> None:
        from services.orchestrator.model import GatewayError

        async def bypassed(payload: Any, item: Any) -> None:
            # A call that bypassed the tracked egress (no record of it): the
            # turn's own figure, here its reservation, is kept, never less.
            item.cout_eur = 0.10
            raise GatewayError("timeout", 504, "Timeout")

        for stream in (False, True):
            path, body = routes(stream)[0]
            with self.subTest(stream=stream):
                row, reply, day = self.settle(bypassed, path, body)
                self.assertEqual((row["charged"], row["state"]), (100000, "complete"))
                self.assertEqual(day, 100000)
                if not stream:
                    self.assertEqual(
                        reply.json()["atlas"]["cost_kind"], "reserved_upper_bound"
                    )


if __name__ == "__main__":
    unittest.main()
