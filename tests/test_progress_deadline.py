"""A call still receiving output is not cut by its own deadline; a silent one is.

Staging 2026-11-13: a Work step streaming its answer (3161 events, 11385
characters) was cut at its fixed 120 s deadline, twice, and the turn failed.
"""

from collections.abc import AsyncGenerator
from decimal import Decimal
from time import monotonic
import asyncio
import json
import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from agent_provider import AgentProvider, AgentRequest
from quality import stream_quality
from serverless_support import environment
from test_quality_gateway import request, result


async def collect(original: AgentRequest) -> list[dict[str, object]]:
    return [event async for event in stream_quality(AgentProvider(), original)]


class ProgressDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_answer_runs_past_its_deadline_up_to_the_wall_clock(
        self,
    ) -> None:
        calls = 0

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            for _ in range(16):
                await asyncio.sleep(0.05)
                yield {"delta": {"content": "word "}}
            yield result("word " * 16, 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            patch("quality.PROGRESS_STALL_SECONDS", 0.25),
        ):
            events = await collect(
                request(timeout=0.3, progress_timeout=2.0, max_tokens=500)
            )
        self.assertEqual(calls, 1)
        self.assertIn("result", events[-1])

    async def test_silent_call_is_still_cut_within_the_wall_clock(self) -> None:
        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            for _ in range(8):
                await asyncio.sleep(0.05)
                yield {"delta": {"content": "word "}}
            await asyncio.sleep(5)
            yield result("never", 20)

        started = monotonic()
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            patch("quality.PROGRESS_STALL_SECONDS", 0.2),
            self.assertLogs("quality", level="WARNING") as logs,
            self.assertRaises(TimeoutError),
        ):
            await collect(request(timeout=0.3, progress_timeout=5.0, max_tokens=500))
        elapsed = monotonic() - started
        # Last output at about 0.4 s, cut one silence window later.
        self.assertGreater(elapsed, 0.5)
        self.assertLess(elapsed, 1.2)
        diagnostic = json.loads(logs.records[0].getMessage())
        self.assertEqual(diagnostic["category"], "timeout")
        self.assertTrue(diagnostic["visible_content"])
        # Unknown usage of the cut attempt stays charged at its full reservation.
        self.assertFalse(diagnostic["final_usage_measured"])
        self.assertGreater(Decimal(diagnostic["reserved_unknown_eur"]), 0)

    async def test_without_progress_timeout_the_request_deadline_still_bounds(
        self,
    ) -> None:
        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            while True:
                await asyncio.sleep(0.05)
                yield {"delta": {"content": "word "}}

        started = monotonic()
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            patch("quality.PROGRESS_STALL_SECONDS", 0.2),
            self.assertLogs("quality", level="WARNING"),
            self.assertRaises(TimeoutError),
        ):
            await collect(request(timeout=0.3, max_tokens=500))
        self.assertLess(monotonic() - started, 0.6)

    async def test_reasoning_progress_keeps_the_first_attempt(self) -> None:
        calls = 0

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            # Past the recovery share (0.8 of 0.5 s) while still thinking.
            for _ in range(9):
                await asyncio.sleep(0.05)
                yield {"delta": {"reasoning_content": "step "}}
            yield {"delta": {"content": "Done."}}
            yield result("Done.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            patch("quality.PROGRESS_STALL_SECONDS", 0.2),
        ):
            events = await collect(request(timeout=0.5, max_tokens=500))
        self.assertEqual(calls, 1)
        self.assertIn("result", events[-1])

    async def test_tool_arguments_being_written_keep_the_call_alive(self) -> None:
        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            for step in range(12):
                await asyncio.sleep(0.05)
                yield {"tool_progress": 2000 + 500 * step}
            yield result("Written.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            patch("quality.PROGRESS_STALL_SECONDS", 0.2),
        ):
            events = await collect(
                request(timeout=0.3, progress_timeout=2.0, max_tokens=500)
            )
        self.assertIn("result", events[-1])
        self.assertNotIn("tool_progress", str(events))

    def test_progress_timeout_needs_the_quality_controller(self) -> None:
        self.assertEqual(request(progress_timeout=900.0).progress_timeout, 900.0)
        with self.assertRaises(ValidationError):
            request(profile=None, progress_timeout=900.0)
        with self.assertRaises(ValidationError):
            request(privacy="strict", progress_timeout=900.0)


class GatewayClientTests(unittest.IsolatedAsyncioTestCase):
    async def sent(self, **settings: object) -> tuple[dict[str, object], float]:
        from services.orchestrator.model import Configuration, GatewayModel

        model = GatewayModel(
            "http://127.0.0.1:9",
            Configuration(
                input_eur_per_mtok=Decimal("1"),
                output_eur_per_mtok=Decimal("1"),
                max_tokens=2048,
            ),
        )
        model.configure_quality("atlas-glm", 1000)
        for key, value in settings.items():
            setattr(model, key, value)
        captured: list[tuple[dict[str, object], float]] = []

        async def post(
            url: str, payload: dict[str, object], timeout: float
        ) -> dict[str, object]:
            captured.append((payload, timeout))
            raise RuntimeError("captured")

        with (
            patch.object(GatewayModel, "post", staticmethod(post)),
            self.assertRaises(Exception),
        ):
            await model.complete([{"role": "user", "content": "Count the files."}], 120)
        return captured[0]

    async def test_work_step_sends_its_wall_clock_and_waits_for_it(self) -> None:
        payload, timeout = await self.sent(progress_deadline=monotonic() + 600)
        self.assertEqual(payload["timeout"], 120)
        self.assertAlmostEqual(float(str(payload["progress_timeout"])), 600, delta=2)
        self.assertAlmostEqual(timeout, 600, delta=2)

    async def test_other_calls_are_unchanged(self) -> None:
        for settings in (
            {},
            {"progress_deadline": monotonic() + 60},
            {"progress_deadline": monotonic() + 600, "privacy": "strict"},
        ):
            payload, timeout = await self.sent(**settings)
            self.assertNotIn("progress_timeout", payload)
            self.assertEqual(timeout, 120)


if __name__ == "__main__":
    unittest.main()
