"""Calls without a provider usage frame are settled on an estimate, never the reserve.

A coding client sends a large max_tokens, so the reservation of one call is large. A
call cut before its usage frame (provider stream cut, timeout, client gone) used to
keep that whole reservation. It is now charged what was really sent and streamed;
refused calls cost nothing; GET /usage keeps the estimated part apart (D5).
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
from time import monotonic
from typing import Any
import unittest
from unittest.mock import patch

import dev_gateway as gateway
from provider_retry import TransientProviderError
from services.orchestrator.api_keys import Store
from services.orchestrator.dev_chat import ChatInput, Output
from services.orchestrator.dev_inference import drive
from services.orchestrator.dev_messages import Wire as MessagesWire
from services.orchestrator.dev_responses import Wire as ResponsesWire
from test_dev_chat import BODY

MAX_TOKENS = 32000
PROMPT = "Fictional repository context. " * 300


def plan(**values: Any) -> gateway.Plan:
    body = {
        "model": "fictional-model",
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": PROMPT}],
    }
    base = gateway.Plan(
        body=json.dumps(body),
        endpoint="https://provider.invalid/v1",
        reserved=0,
        input_rate=Decimal("1.19"),
        output_rate=Decimal("3.74"),
        cached_input_rate=Decimal("0.119"),
        provider="fictional",
        model="fictional-model",
    )
    bound = base.charge(len(base.body.encode()), MAX_TOKENS)
    return replace(base, reserved=bound, attempt_bound=bound, **values)


def frame(text: str) -> bytes:
    chunk = {"choices": [{"index": 0, "delta": {"content": text}}]}
    return b"data: " + json.dumps(chunk).encode() + b"\n\n"


def finished(prompt: int, completion: int, cached: int = 0) -> bytes:
    usage: dict[str, Any] = {"prompt_tokens": prompt, "completion_tokens": completion}
    if cached:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    end = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    return (
        b"data: "
        + json.dumps(end).encode()
        + b"\n\ndata: "
        + json.dumps({"choices": [], "usage": usage}).encode()
        + b"\n\ndata: [DONE]\n\n"
    )


class EstimateTests(unittest.TestCase):
    def test_estimate_counts_body_and_streamed_text_never_max_tokens(self) -> None:
        current = plan()
        attempt = gateway.Attempt(current, output_bytes=300)
        cost, incoming, outgoing = attempt.estimate()
        self.assertEqual(incoming, -(-len(current.body.encode()) // 3))
        self.assertEqual(outgoing, 100)
        # No cache discount for tokens nobody measured.
        self.assertEqual(cost, current.charge(incoming, outgoing))
        self.assertLess(cost, current.reserved / 10)

    def test_streamed_output_is_capped_by_max_tokens(self) -> None:
        attempt = gateway.Attempt(plan(), output_bytes=10 * 3 * MAX_TOKENS)
        self.assertEqual(attempt.estimate()[2], MAX_TOKENS)

    def test_refused_attempt_costs_nothing(self) -> None:
        attempt = gateway.Attempt(plan(), output_bytes=0, refused=True)
        self.assertEqual(attempt.estimate(), (0, 0, 0))


class SettlementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root / "keys.sqlite", "fictional-pepper-" * 4)
        row, _ = self.store.create(
            org_id="fictional-org",
            member_id="fictional-member",
            subject="tester@example.invalid",
            budget_user="fictional-user",
            name="billing",
            scopes=["model"],
            created_by="operator",
            daily_requests=20,
        )
        self.key = str(row["id"])

    async def run_call(
        self, transport: Any, current: gateway.Plan, close_after: int = 0
    ) -> tuple[str, list[dict[str, Any]]]:
        request = ChatInput.model_validate(BODY | {"stream": True})
        request_id = self.store.reserve(self.key, current.reserved)
        frames = drive(
            self.store,
            self.key,
            request_id,
            current,
            Output(request_id, request),
            monotonic() + 60,
        )
        seen: list[dict[str, Any]] = []
        with patch.object(gateway, "transport", transport):
            if close_after:
                for _ in range(close_after):
                    seen.append(await frames.__anext__())
                await frames.aclose()
            else:
                seen = [item async for item in frames]
        return request_id, seen

    def row(self, request_id: str) -> sqlite3.Row:
        with self.store.connection() as db:
            found: sqlite3.Row | None = db.execute(
                "SELECT * FROM api_key_requests WHERE id=?", (request_id,)
            ).fetchone()
        assert found is not None
        return found

    async def test_stream_cut_before_usage_is_estimated(self) -> None:
        current = plan()

        async def cut(_: gateway.Plan) -> AsyncIterator[bytes]:
            yield frame("x" * 600)
            raise TransientProviderError("provider_error", started=True)

        request_id, seen = await self.run_call(cut, current)
        self.assertIn("error", seen[-1])
        row = self.row(request_id)
        expected = gateway.Attempt(current, output_bytes=600).estimate()
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["charged"], expected[0])
        self.assertEqual(row["estimated"], expected[0])
        self.assertEqual((row["input_tokens"], row["output_tokens"]), expected[1:])
        self.assertLess(row["charged"], current.reserved / 10)
        usage = self.store.usage(self.key)
        self.assertEqual(usage["charged_micro_eur"], expected[0])
        self.assertEqual(usage["unknown_micro_eur"], expected[0])
        self.assertEqual(usage["estimated_micro_eur"], expected[0])

    async def test_client_gone_is_estimated(self) -> None:
        current = plan()

        async def slow(_: gateway.Plan) -> AsyncIterator[bytes]:
            yield frame("y" * 30)
            yield frame("y" * 30)
            yield finished(4000, 50)

        request_id, _ = await self.run_call(slow, current, close_after=1)
        row = self.row(request_id)
        self.assertEqual(row["state"], "unknown")
        self.assertGreater(row["charged"], 0)
        self.assertLess(row["charged"], current.reserved / 10)

    async def test_refused_call_costs_nothing_and_is_measured(self) -> None:
        current = plan()

        async def refused(_: gateway.Plan) -> AsyncIterator[bytes]:
            if current.reserved:
                raise gateway.ProviderRejection("provider_error")
            yield b""

        request_id, seen = await self.run_call(refused, current)
        self.assertIn("error", seen[-1])
        row = self.row(request_id)
        self.assertEqual(
            (row["state"], row["charged"], row["estimated"]), ("complete", 0, 0)
        )

    async def test_measured_call_bills_cached_rate_and_estimates_nothing(self) -> None:
        current = plan()

        async def complete(_: gateway.Plan) -> AsyncIterator[bytes]:
            yield frame("done")
            yield finished(10000, 20, cached=9000)

        request_id, _ = await self.run_call(complete, current)
        row = self.row(request_id)
        self.assertEqual(row["state"], "complete")
        self.assertEqual(row["estimated"], 0)
        # 1000 * 1.19 + 9000 * 0.119 + 20 * 3.74 micro-EUR, rounded up.
        self.assertEqual(row["charged"], 2336)
        self.assertEqual(row["charged"], current.charge(10000, 20, 9000))
        self.assertEqual(self.store.usage(self.key)["estimated_micro_eur"], 0)

    async def test_fallback_after_cut_primary_marks_estimated_part(self) -> None:
        fallback = replace(plan(), provider="second", model="second-model")
        current = plan(fallback=fallback)

        async def transport(target: gateway.Plan) -> AsyncIterator[bytes]:
            if target.provider == "fictional":
                yield b": waiting\n\n"
                raise TransientProviderError("provider_timeout", started=True)
            yield frame("ok")
            yield finished(1000, 2)

        request_id, _ = await self.run_call(transport, current)
        row = self.row(request_id)
        primary = gateway.Attempt(current).estimate()[0]
        self.assertEqual(row["state"], "complete")
        self.assertEqual(row["estimated"], primary)
        self.assertEqual(row["charged"], fallback.charge(1000, 2) + primary)


class MigrationTests(unittest.TestCase):
    def test_earlier_unmeasured_rows_count_as_estimated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keys.sqlite"
            store = Store(path, "fictional-pepper-" * 4)
            row, _ = store.create(
                org_id="fictional-org",
                member_id="fictional-member",
                subject="tester@example.invalid",
                budget_user="fictional-user",
                name="legacy",
                scopes=["model"],
                created_by="operator",
            )
            kept = store.reserve(str(row["id"]), 300000)
            store.finish(kept, None)
            # The table as it was before the estimated column.
            columns = (
                "id,key_id,day,week,reserved,charged,input_tokens,output_tokens,"
                "code_executions,state"
            )
            with store.connection() as db:
                db.execute(
                    "CREATE TABLE earlier (id TEXT PRIMARY KEY, key_id TEXT NOT NULL,"
                    " day TEXT NOT NULL, week TEXT NOT NULL, reserved INTEGER NOT NULL,"
                    " charged INTEGER NOT NULL, input_tokens INTEGER,"
                    " output_tokens INTEGER, code_executions INTEGER NOT NULL"
                    " DEFAULT 0, state TEXT NOT NULL)"
                )
                db.execute(
                    f"INSERT INTO earlier ({columns}) SELECT {columns} FROM api_key_requests"
                )
                db.execute("DROP TABLE api_key_requests")
                db.execute("ALTER TABLE earlier RENAME TO api_key_requests")
            reopened = Store(path, "fictional-pepper-" * 4)
            usage = reopened.usage(str(row["id"]))
            self.assertEqual(usage["unknown_micro_eur"], 300000)
            self.assertEqual(usage["estimated_micro_eur"], 300000)


class CacheReportTests(unittest.TestCase):
    def usage(self) -> dict[str, Any]:
        return {
            "prompt_tokens": 10000,
            "completion_tokens": 20,
            "total_tokens": 10020,
            "prompt_tokens_details": {"cached_tokens": 9000},
        }

    def test_messages_reports_cache_reads_apart(self) -> None:
        output = Output("request", ChatInput.model_validate(BODY))
        output.usage = self.usage()
        usage = MessagesWire(output, {}).result()["usage"]
        self.assertEqual(usage["input_tokens"], 1000)
        self.assertEqual(usage["cache_read_input_tokens"], 9000)
        self.assertEqual(usage["cache_creation_input_tokens"], 0)
        self.assertEqual(usage["output_tokens"], 20)

    def test_messages_without_cache_details(self) -> None:
        output = Output("request", ChatInput.model_validate(BODY))
        output.usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        usage = MessagesWire(output, {}).result()["usage"]
        self.assertEqual(
            (usage["input_tokens"], usage["cache_read_input_tokens"]), (10, 0)
        )

    def test_responses_reports_cached_tokens(self) -> None:
        output = Output("request", ChatInput.model_validate(BODY))
        output.usage = self.usage()
        usage = ResponsesWire(output, {}).result()["usage"]
        self.assertEqual(usage["input_tokens"], 10000)
        self.assertEqual(usage["input_tokens_details"], {"cached_tokens": 9000})


if __name__ == "__main__":
    unittest.main()
