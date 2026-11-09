"""Tests for the opencode Work engine driver (no network beyond a local fake)."""

import asyncio
import json
import unittest
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

from services.orchestrator.work_opencode import (
    OpencodeDriver,
    map_event,
    permission_response,
    step_usage,
)


def sse(*payloads: dict[str, Any]) -> bytes:
    return b"".join(b"data: " + json.dumps(p).encode() + b"\n\n" for p in payloads)


class MapEventTests(unittest.TestCase):
    def test_tool_part_running_and_completed(self) -> None:
        base = {"type": "message.part.updated", "properties": {"sessionID": "s1"}}
        running = dict(
            base,
            properties={
                "sessionID": "s1",
                "part": {
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": "running", "input": {"command": "ls"}},
                },
            },
        )
        event = map_event(running)
        assert event is not None
        self.assertEqual(event["kind"], "tool")
        self.assertEqual(event["tool"], "bash")
        self.assertEqual(event["status"], "running")
        self.assertIn("ls", event["input"])
        self.assertEqual(event["session_id"], "s1")
        completed = dict(
            running,
            properties={
                "sessionID": "s1",
                "part": {
                    "type": "tool",
                    "tool": "read",
                    "state": {"status": "completed", "output": "file body"},
                },
            },
        )
        event = map_event(completed)
        assert event is not None
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["output"], "file body")
        errored = dict(
            completed,
            properties={
                "sessionID": "s1",
                "part": {
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": "error", "output": "boom"},
                },
            },
        )
        errored_event = map_event(errored)
        assert errored_event is not None
        self.assertEqual(errored_event["status"], "error")

    def test_text_delta(self) -> None:
        event = map_event(
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "s1",
                    "part": {"type": "text", "text": "hello"},
                },
            }
        )
        self.assertEqual(event, {"kind": "text", "text": "hello", "session_id": "s1"})
        blank = map_event(
            {
                "type": "message.part.updated",
                "properties": {"part": {"type": "text", "text": "  "}},
            }
        )
        self.assertIsNone(blank)

    def test_session_idle_error_and_permission(self) -> None:
        self.assertEqual(
            map_event({"type": "session.idle", "properties": {"sessionID": "s1"}}),
            {"kind": "turn_end", "session_id": "s1"},
        )
        error = map_event(
            {"type": "session.error", "properties": {"error": "provider down"}}
        )
        assert error is not None
        self.assertEqual(error["kind"], "error")
        self.assertEqual(error["detail"], "provider down")
        permission = map_event(
            {
                "type": "permission.asked",
                "properties": {
                    "id": "perm-1",
                    "sessionID": "s1",
                    "type": "bash",
                    "pattern": "rm -rf /",
                },
            }
        )
        self.assertEqual(
            permission,
            {
                "kind": "permission",
                "permission_id": "perm-1",
                "session_id": "s1",
                "tool": "bash",
                "pattern": "rm -rf /",
            },
        )

    def test_unknown_payloads(self) -> None:
        self.assertIsNone(map_event({"type": "server.connected", "properties": {}}))
        self.assertIsNone(map_event({}))
        self.assertIsNone(
            map_event(
                {
                    "type": "message.part.updated",
                    "properties": {"part": {"type": "step-start"}},
                }
            )
        )


class StepUsageTests(unittest.TestCase):
    def test_step_finish(self) -> None:
        usage = step_usage(
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "type": "step-finish",
                        "tokens": {"input": 10, "output": 5},
                        "cost": 0.01,
                    }
                },
            }
        )
        assert usage is not None
        self.assertEqual(usage[:2], (10, 5))
        self.assertEqual(float(usage[2]), 0.01)
        self.assertIsNone(
            step_usage(
                {
                    "type": "message.part.updated",
                    "properties": {"part": {"type": "text", "text": "hi"}},
                }
            )
        )


class PermissionResponseTests(unittest.TestCase):
    def test_auto_allowed_once(self) -> None:
        for name in ("read", "grep", "glob", "list", "todowrite"):
            self.assertEqual(permission_response(name, allowed=False), "once")

    def test_bash_and_unknown_need_approval(self) -> None:
        self.assertIsNone(permission_response("bash", allowed=True))
        self.assertIsNone(permission_response("edit", allowed=True))
        self.assertIsNone(permission_response("anything-else", allowed=True))

    def test_external_directory_always_reject(self) -> None:
        self.assertEqual(
            permission_response("external_directory", allowed=True), "reject"
        )

    def test_webfetch_reject_when_research_disabled(self) -> None:
        self.assertEqual(permission_response("webfetch", allowed=False), "reject")
        self.assertIsNone(permission_response("webfetch", allowed=True))


class FakeOpencode:
    """Minimal fake opencode server: SSE stream plus the REST endpoints."""

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/session", self.create_session)
        app.router.add_post("/session/{sid}/prompt_async", self.prompt_async)
        app.router.add_post("/session/{sid}/permissions/{pid}", self.permission)
        app.router.add_post("/session/{sid}/abort", self.abort)
        app.router.add_get("/event", self.stream)
        return app

    async def create_session(self, request: web.Request) -> web.Response:
        return web.json_response({"id": "sess-1"})

    async def prompt_async(self, request: web.Request) -> web.Response:
        self.prompts.append(await request.json())
        return web.json_response({})

    async def permission(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.permissions.append((request.match_info["pid"], body["response"]))
        return web.json_response({})

    async def abort(self, request: web.Request) -> web.Response:
        self.aborted.append(request.match_info["sid"])
        return web.json_response({})

    async def stream(self, request: web.Request) -> web.StreamResponse:
        self.stream_opened.set()
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        if self.hold_stream:
            await self.hold_stream.wait()
        await response.write(sse(*self.events))
        await response.write_eof()
        return response

    def __init__(self, events: list[dict[str, Any]], hold_stream: bool = False):
        self.events = events
        self.hold_stream = asyncio.Event() if hold_stream else None
        self.stream_opened = asyncio.Event()
        self.aborted: list[str] = []
        self.permissions: list[tuple[str, str]] = []
        self.prompts: list[dict[str, Any]] = []


class DriverTests(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self, fake: FakeOpencode, **kwargs: Any
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        emitted: list[dict[str, Any]] = []

        async def emit(event: dict[str, Any]) -> None:
            emitted.append(event)

        async with TestServer(fake.app()) as server:
            driver = OpencodeDriver(str(server.make_url("/")), "pw", emit, **kwargs)
            result = await driver.run("fix the tests")
        return result, emitted

    async def test_full_turn_maps_events_and_answers_permission(self) -> None:
        fake = FakeOpencode(
            [
                {
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": "sess-1",
                        "part": {
                            "type": "tool",
                            "tool": "bash",
                            "state": {"status": "running", "input": {"command": "ls"}},
                        },
                    },
                },
                {
                    "type": "permission.asked",
                    "properties": {
                        "id": "perm-1",
                        "sessionID": "sess-1",
                        "type": "bash",
                        "pattern": "ls",
                    },
                },
                {
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": "sess-1",
                        "part": {
                            "type": "step-finish",
                            "tokens": {"input": 100, "output": 40},
                            "cost": 0.02,
                        },
                    },
                },
                {
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": "sess-1",
                        "part": {"type": "text", "text": "all green"},
                    },
                },
                {"type": "session.idle", "properties": {"sessionID": "sess-1"}},
            ]
        )
        asked: list[dict[str, Any]] = []

        async def approve(request: dict[str, Any]) -> bool:
            asked.append(request)
            return True

        result, emitted = await self._run(fake, approve=approve)
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(result["cost"], 0.02)
        self.assertEqual(result["input_tokens"], 100)
        self.assertEqual(result["output_tokens"], 40)
        self.assertEqual(
            [e["kind"] for e in emitted], ["tool", "permission", "text", "turn_end"]
        )
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0]["permission_id"], "perm-1")
        self.assertEqual(fake.permissions, [("perm-1", "once")])
        self.assertEqual(fake.aborted, [])
        self.assertEqual(fake.prompts[0]["parts"][0]["text"], "fix the tests")

    async def test_budget_abort_calls_abort_endpoint(self) -> None:
        fake = FakeOpencode(
            [
                {
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": "sess-1",
                        "part": {
                            "type": "step-finish",
                            "tokens": {"input": 10, "output": 5},
                            "cost": 0.5,
                        },
                    },
                },
                {
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": "sess-1",
                        "part": {"type": "text", "text": "still working"},
                    },
                },
                {"type": "session.idle", "properties": {"sessionID": "sess-1"}},
            ]
        )
        result, _ = await self._run(fake, cost_limit=0.3)
        self.assertEqual(result["outcome"], "aborted_budget")
        self.assertEqual(fake.aborted, ["sess-1"])

    async def test_time_abort_calls_abort_endpoint(self) -> None:
        fake = FakeOpencode(
            [
                {
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": "sess-1",
                        "part": {"type": "text", "text": "working"},
                    },
                },
                {"type": "session.idle", "properties": {"sessionID": "sess-1"}},
            ]
        )
        result, _ = await self._run(fake, time_limit=0.001)
        self.assertEqual(result["outcome"], "aborted_time")
        self.assertEqual(fake.aborted, ["sess-1"])

    async def test_session_error_outcome(self) -> None:
        fake = FakeOpencode(
            [
                {
                    "type": "session.error",
                    "properties": {"sessionID": "sess-1", "error": "boom"},
                }
            ]
        )
        result, _ = await self._run(fake)
        self.assertEqual(result["outcome"], "error")

    async def test_event_stream_opened_before_prompt(self) -> None:
        fake = FakeOpencode(
            [{"type": "session.idle", "properties": {"sessionID": "sess-1"}}]
        )
        prompt_seen = asyncio.Event()

        original_prompt = fake.prompt_async

        async def prompt_async(request: web.Request) -> web.Response:
            self.assertTrue(fake.stream_opened.is_set())
            prompt_seen.set()
            return await original_prompt(request)

        fake.prompt_async = prompt_async  # type: ignore[method-assign]
        result, _ = await self._run(fake)
        self.assertTrue(prompt_seen.is_set())
        self.assertEqual(result["outcome"], "complete")

    async def test_foreign_session_events_ignored(self) -> None:
        fake = FakeOpencode(
            [
                {"type": "session.idle", "properties": {"sessionID": "other"}},
                {
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": "other",
                        "part": {"type": "text", "text": "not ours"},
                    },
                },
                {
                    "type": "message.part.updated",
                    "properties": {
                        "part": {
                            "type": "text",
                            "text": "ours",
                            "sessionID": "sess-1",
                        }
                    },
                },
                {"type": "session.idle", "properties": {"sessionID": "sess-1"}},
            ]
        )
        result, emitted = await self._run(fake)
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual([e["kind"] for e in emitted], ["text", "turn_end"])
        self.assertEqual(emitted[0]["text"], "ours")

    async def test_budget_checked_on_step_finish_payload(self) -> None:
        fake = FakeOpencode(
            [
                {
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": "sess-1",
                        "part": {
                            "type": "step-finish",
                            "tokens": {"input": 10, "output": 5},
                            "cost": 0.5,
                        },
                    },
                },
                {"type": "session.idle", "properties": {"sessionID": "sess-1"}},
            ]
        )
        result, emitted = await self._run(fake, cost_limit=0.3)
        self.assertEqual(result["outcome"], "aborted_budget")
        self.assertEqual(fake.aborted, ["sess-1"])
        self.assertEqual(emitted, [])

    async def test_silent_stream_aborts_on_time(self) -> None:
        fake = FakeOpencode([], hold_stream=True)
        emitted: list[dict[str, Any]] = []

        async def emit(event: dict[str, Any]) -> None:
            emitted.append(event)

        async with TestServer(fake.app()) as server:
            driver = OpencodeDriver(
                str(server.make_url("/")), "pw", emit, time_limit=0.2
            )
            result = await asyncio.wait_for(driver.run("fix the tests"), timeout=10)
        self.assertEqual(result["outcome"], "aborted_time")
        self.assertEqual(fake.aborted, ["sess-1"])

    def test_permission_patterns_list(self) -> None:
        event = map_event(
            {
                "type": "permission.asked",
                "properties": {
                    "id": "perm-9",
                    "sessionID": "s1",
                    "type": "bash",
                    "patterns": ["rm -rf /", "git push"],
                    "pattern": "ignored",
                    "title": "also ignored",
                },
            }
        )
        assert event is not None
        self.assertEqual(event["pattern"], "rm -rf /, git push")


if __name__ == "__main__":
    unittest.main()


class StreamEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_stream_ends_the_turn_as_error(self) -> None:
        async def create(request: web.Request) -> web.Response:
            return web.json_response({"id": "sess-1"})

        async def prompt(request: web.Request) -> web.Response:
            return web.json_response({})

        async def stream(request: web.Request) -> web.StreamResponse:
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b'data: {"type": "server.connected"}\n\n')
            return response

        app = web.Application()
        app.router.add_post("/session", create)
        app.router.add_post("/session/sess-1/prompt_async", prompt)
        app.router.add_get("/event", stream)

        async def emit(event: dict[str, Any]) -> None:
            return None

        async with TestServer(app) as server:
            driver = OpencodeDriver(str(server.make_url("")), "pw", emit)
            result = await asyncio.wait_for(driver.run("hello"), timeout=5)
        self.assertEqual(result["outcome"], "error")


class StepCostTests(unittest.TestCase):
    def test_tokens_priced_when_opencode_reports_zero(self) -> None:
        from decimal import Decimal

        async def emit(event: dict[str, Any]) -> None:
            return None

        driver = OpencodeDriver(
            "http://x", "pw", emit, token_prices=(Decimal("2"), Decimal("8"))
        )
        self.assertEqual(
            driver._step_cost((1_000_000, 500_000, Decimal(0))), Decimal("6")
        )
        self.assertEqual(driver._step_cost((10, 10, Decimal("0.5"))), Decimal("0.5"))
        unpriced = OpencodeDriver("http://x", "pw", emit)
        self.assertEqual(unpriced._step_cost((10, 10, Decimal(0))), Decimal(0))
