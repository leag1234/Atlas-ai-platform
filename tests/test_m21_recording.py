"""Interrupted real streams remain replayable without inventing a completion."""

import asyncio
import unittest
from collections.abc import AsyncGenerator

from m21_gate import assert_derivation, assert_guitar_geometry
from provider_recording import (
    ExactHistory,
    canonical_json,
    capture_stream,
    capture_tool,
    match_request,
    mismatch_report,
    plan_refresh,
    replay_stream,
    request_manifest,
)


class RecordingTests(unittest.IsolatedAsyncioTestCase):
    def test_acquisition_reuse_requires_exact_unused_request(self) -> None:
        original = [
            {
                "kind": "stream",
                "request": {"prompt": "original"},
                "response": [{"text": "real"}],
            }
        ]
        history = ExactHistory(original)
        self.assertIsNone(history.take("stream", {"prompt": "changed"}))
        self.assertIsNone(history.take("image", {"prompt": "original"}))
        result = history.take("stream", {"prompt": "original"})
        self.assertEqual(result, [{"text": "real"}])
        result[0]["text"] = "mutated"
        self.assertEqual(original[0]["response"], [{"text": "real"}])
        self.assertIsNone(history.take("stream", {"prompt": "original"}))

    async def test_http_status_error_is_recorded_without_provider_body(self) -> None:
        saved: list[object] = []

        async def tool() -> object:
            raise ValueError("http_404")

        with self.assertRaisesRegex(ValueError, "^http_404$"):
            await capture_tool(tool(), saved.append)
        self.assertEqual(
            saved, [{"recorded_exception": "ValueError", "message": "http_404"}]
        )

    async def test_cancelled_tool_is_saved_before_propagating(self) -> None:
        async def tool() -> object:
            raise asyncio.CancelledError()

        saved: list[object] = []
        with self.assertRaises(asyncio.CancelledError):
            await capture_tool(tool(), saved.append)
        self.assertEqual(saved, [{"recorded_exception": "TimeoutError", "message": ""}])

    async def test_tool_errors_retain_only_safe_fixed_codes(self) -> None:
        async def tool(message: str) -> object:
            raise ValueError(message)

        saved: list[object] = []
        with self.assertRaises(ValueError):
            await capture_tool(tool("robots_denied"), saved.append)
        self.assertEqual(
            saved, [{"recorded_exception": "ValueError", "message": "robots_denied"}]
        )
        with self.assertRaises(AssertionError):
            await capture_tool(tool("private provider payload"), saved.append)
        self.assertEqual(len(saved), 1)

    async def test_legacy_recorded_frames_remain_exact(self) -> None:
        frames = [
            {"delta": {"content": "Recorded answer"}},
            {"result": {"text": "Recorded answer"}},
        ]
        self.assertEqual([event async for event in replay_stream(frames)], frames)

    async def test_interrupted_trace_then_error_roundtrip(self) -> None:
        async def upstream() -> AsyncGenerator[dict[str, object], None]:
            yield {"delta": {"reasoning_content": "Observed trace"}}
            raise TimeoutError("private transport detail")

        saved: list[object] = []
        with self.assertRaises(TimeoutError):
            _ = [event async for event in capture_stream(upstream(), saved.append)]
        self.assertEqual(len(saved), 1)
        self.assertNotIn("private transport detail", str(saved))
        received = []
        with self.assertRaises(TimeoutError):
            async for event in replay_stream(saved[0]):
                received.append(event)
        self.assertEqual(received, [{"delta": {"reasoning_content": "Observed trace"}}])

    async def test_cancellation_records_interruption_and_propagates(self) -> None:
        async def upstream() -> AsyncGenerator[dict[str, object], None]:
            yield {"delta": {"reasoning_content": "Observed trace"}}
            raise asyncio.CancelledError()

        saved: list[object] = []
        with self.assertRaises(asyncio.CancelledError):
            _ = [event async for event in capture_stream(upstream(), saved.append)]
        with self.assertRaises(TimeoutError):
            _ = [event async for event in replay_stream(saved[0])]

    async def test_completed_stream_is_recorded_once(self) -> None:
        async def upstream() -> AsyncGenerator[dict[str, object], None]:
            yield {"delta": {"content": "Answer"}}
            yield {"result": {"text": "Answer"}}

        saved: list[object] = []
        original = [event async for event in capture_stream(upstream(), saved.append)]
        replayed = [event async for event in replay_stream(saved[0])]
        self.assertEqual(len(saved), 1)
        self.assertEqual(replayed, original)


class ManifestMatcherTests(unittest.TestCase):
    def test_canonical_json_retains_unicode_and_sorts_nested_tools(self) -> None:
        left = {
            "tools": [{"function": {"name": "cherche", "parameters": {"é": 1}}}],
            "messages": [{"content": "Bonjour 🌍", "role": "user"}],
        }
        right = {
            "messages": [{"role": "user", "content": "Bonjour 🌍"}],
            "tools": [{"function": {"parameters": {"é": 1}, "name": "cherche"}}],
        }
        self.assertEqual(canonical_json(left), canonical_json(right))

    def test_nested_message_and_tool_difference_has_exact_json_path(self) -> None:
        recorded = {
            "messages": [{"role": "system", "content": "rule"}],
            "tools": [{"function": {"name": "search", "parameters": {"n": 3}}}],
        }
        current = {
            "messages": [{"role": "system", "content": "changed"}],
            "tools": [{"function": {"name": "search", "parameters": {"n": 4}}}],
        }
        result = match_request("stream", recorded, current)
        self.assertEqual(result["matched"], "none")
        diffs = result["diffs"]
        assert isinstance(diffs, list)
        self.assertTrue(
            any(str(diff).startswith("messages[0].content:") for diff in diffs)
        )
        self.assertIn("tools[0].function.parameters.n: 3 -> 4", diffs)

    def test_timeout_is_the_only_default_normalisation(self) -> None:
        result = match_request(
            "stream",
            {"timeout": 10, "budget_eur": 0.10},
            {"timeout": 20, "budget_eur": 0.50},
        )
        self.assertEqual(result["matched"], "none")
        diffs = result["diffs"]
        assert isinstance(diffs, list)
        self.assertIn("budget_eur: 0.1 -> 0.5", diffs)
        with self.assertRaisesRegex(ValueError, "unsafe_manifest_exclusion:budget_eur"):
            request_manifest("stream", {"budget_eur": 0.10}, ("budget_eur",))

    def test_mismatch_report_hashes_prompt_and_keeps_budget_delta(self) -> None:
        match = match_request(
            "stream",
            {"messages": [{"content": "old", "role": "system"}], "budget_eur": 0.10},
            {"messages": [{"content": "new", "role": "system"}], "budget_eur": 0.50},
        )
        self.assertEqual(
            mismatch_report(match),
            ["budget_eur: 0.1 -> 0.5", "messages[0].content: prompt hash changed"],
        )

    def test_duplicate_rows_are_each_reused_once_and_planned(self) -> None:
        rows = [
            {"kind": "search", "request": {"query": "same"}, "response": 1},
            {"kind": "search", "request": {"query": "same"}, "response": 2},
        ]
        history = ExactHistory(rows)
        self.assertEqual(history.take("search", {"query": "same"}), 1)
        self.assertEqual(history.take("search", {"query": "same"}), 2)
        self.assertIsNone(history.take("search", {"query": "same"}))
        plan = plan_refresh(
            rows,
            [
                {"kind": "search", "request": {"query": "same"}},
                {"kind": "search", "request": {"query": "changed"}},
            ],
        )
        self.assertEqual(plan["reusable"], 1)
        self.assertEqual(plan["live_capture"], 1)
        planned_rows = plan["rows"]
        assert isinstance(planned_rows, list)
        first, second = planned_rows
        assert isinstance(first, dict)
        assert isinstance(second, dict)
        self.assertEqual(first["action"], "reuse")
        self.assertEqual(second["action"], "live_capture")
        reason = second["reason"]
        assert isinstance(reason, list)
        self.assertIn('query: "same" -> "changed"', reason)


class AnswerQualityTests(unittest.TestCase):
    def test_urls_and_instruction_substrings_cannot_pass_derivation(self) -> None:
        wrong = "<details>OTIR https://example.org/2021/</details> Une routine OTIR prend 16 cycles, 250000 octets/s."
        with self.assertRaises(AssertionError):
            assert_derivation(wrong)
        with self.assertRaises(AssertionError):
            assert_derivation(
                "OUTI and OTIR take 16 and 21 cycles. Rate is 200000 bits/s."
            )
        assert_derivation(
            "OUTI: 16 cycles, 4000000 / 16 = 250000 bytes/s. OTIR repeats in 21 cycles: 4000000 / 21 = 190476 bytes/s, final iteration 16."
        )


class GeometryTests(unittest.TestCase):
    def test_repeating_owner_guess_does_not_validate_mirrored_geometry(self) -> None:
        with self.assertRaisesRegex(AssertionError, "mirrored_guitar_geometry"):
            assert_guitar_geometry(
                "Le sillet de tête à gauche. Conclusion: Ré mineur, notes ré-fa-la, case 5."
            )
        assert_guitar_geometry(
            "Le sillet est à droite de la photo; les cases se comptent vers la gauche."
        )

    def test_repeating_rate_requires_terminal_iteration_and_correct_counter(
        self,
    ) -> None:
        for answer in (
            "OUTI 16, OTIR 21: 250000 and190476 bytes/s.",
            "OUTI 16, OTIR 21: 250000 and 190476 bytes/s. Final iteration16, repeat while BC ≠ 0.",
        ):
            with self.assertRaises(AssertionError):
                assert_derivation(answer)
