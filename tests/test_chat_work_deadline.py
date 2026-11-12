"""Work behind a chat turn ends before the chat deadline and explains why it stopped.

Staging 2026-11-12: a Go function with tests, on a runner without Go, ran in Work
until the chat stream's 300 s deadline cut it; Work's own limit (1200 s) was never
reached, so no explanation came, and the cut stream's error was French in an
English conversation.
"""

import asyncio
import os
import unittest
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock, patch

from services.orchestrator import work_opencode_turn as turn_module
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.deadline import remaining, request_deadline


def request(text: str, **kwargs: object) -> ChatRequest:
    return ChatRequest.model_validate(
        {"messages": [{"role": "user", "content": text}], "ui_locale": "en", **kwargs}
    )


def scratch_patches(work: Any) -> list[Any]:
    return [
        patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}),
        patch("services.orchestrator.chat_work.work_gate_open", return_value=True),
        patch(
            "services.orchestrator.chat_work._identity",
            new=AsyncMock(
                return_value={
                    "memberships": [{"org_id": "123e4567-e89b-12d3-a456-426614174000"}]
                }
            ),
        ),
        patch(
            "services.orchestrator.chat_work._ensure_scratch_project",
            new=AsyncMock(return_value="123e4567-e89b-12d3-a456-426614174002"),
        ),
        patch(
            "services.orchestrator.chat_work._find_binding",
            new=AsyncMock(
                return_value={
                    "conversation_id": "123e4567-e89b-12d3-a456-426614174003",
                    "work_session_id": "123e4567-e89b-12d3-a456-426614174004",
                }
            ),
        ),
        patch("services.orchestrator.code_mode.process_work", new=work),
    ]


async def escalate(req: ChatRequest, work: Any) -> tuple[bool, Any]:
    from contextlib import ExitStack

    from services.orchestrator.chat_work import escalate_to_work
    from services.orchestrator.interactions import Interaction

    req.__dict__["native_chat_id"] = "123e4567-e89b-12d3-a456-426614174020"
    req.__dict__["artifact_owner"] = "user@example.com"
    item = Interaction()
    with ExitStack() as stack:
        for patcher in scratch_patches(work):
            stack.enter_context(patcher)
        handed = await escalate_to_work(req, item)
    return handed, item


class RemainingTests(unittest.IsolatedAsyncioTestCase):
    async def test_seconds_left_follow_the_current_deadline(self) -> None:
        self.assertIsNone(remaining())
        async with request_deadline(300):
            left = remaining()
            assert left is not None
            self.assertTrue(299 <= left <= 300)
        self.assertIsNone(remaining())


class ChatWorkTimeLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_work_gets_the_chat_deadline_less_the_answer_margin(self) -> None:
        from services.orchestrator.chat_work import CHAT_ANSWER_SECONDS

        seen: list[Any] = []

        async def work(r, i, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(kwargs.get("wall_clock"))
            i.state, i.reponse = "done", "Tests pass."

        async with request_deadline(300):
            handed, _ = await escalate(
                request("Write a Python script that sorts a list and run it"), work
            )
        self.assertTrue(handed)
        self.assertEqual(len(seen), 1)
        self.assertTrue(
            300 - CHAT_ANSWER_SECONDS - 1 <= seen[0] <= 300 - CHAT_ANSWER_SECONDS
        )

    async def test_without_a_chat_deadline_work_keeps_its_own_limit(self) -> None:
        seen: list[Any] = []

        async def work(r, i, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(kwargs.get("wall_clock"))
            i.state, i.reponse = "done", "Tests pass."

        await escalate(
            request("Write a Python script that sorts a list and run it"), work
        )
        self.assertEqual(seen, [None])

    async def test_a_turn_stopped_by_its_own_timeout_is_explained_once(self) -> None:
        calls: list[int] = []

        async def work(r, i, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(1)
            async with asyncio.timeout(0):
                await asyncio.sleep(1)

        req = request(
            "Write a function in Rust that counts vowels, with unit tests, "
            "and run the tests to show they pass",
            lang="en",
        )
        async with request_deadline(300):
            handed, item = await escalate(req, work)
        self.assertTrue(handed)
        self.assertEqual(calls, [1])
        self.assertEqual((item.state, item.task_type), ("done", "work"))
        self.assertIn("the time limit was reached", item.reponse)

    async def test_a_file_turn_out_of_time_is_explained_not_handed_back(self) -> None:
        # The chat has no time left to answer a stopped file turn by itself.
        async def work(r, i, **kwargs):  # type: ignore[no-untyped-def]
            i.state, i.reponse = "failed", "Work failed: time_limit"
            i.erreurs.append("time_limit")

        req = request("Fais-moi un document Word sur le télétravail", lang="fr")
        handed, item = await escalate(req, work)
        self.assertTrue(handed)
        self.assertIn("la limite de temps a été atteinte", item.reponse)


class EngineTimeLimitTests(unittest.IsolatedAsyncioTestCase):
    async def _limit(self, wall_clock: float, startup: float) -> float:
        class Limits:
            cost = 3
            wall_clock = 0.0

        Limits.wall_clock = wall_clock
        driver = mock.Mock()
        driver.run = AsyncMock(return_value={"outcome": "aborted_time"})
        driver_cls = mock.Mock(return_value=driver)
        clock = iter([1000.0, 1000.0 + startup])

        class Engine:
            url, password, name = "http://engine:4096", "pw", "atlas-opencode-test"

        class Tools:
            async def emit(self, kind: str, **data: Any) -> None:
                return None

        class Item:
            erreurs: list[str] = []

        with (
            mock.patch.object(turn_module, "start", AsyncMock(return_value=Engine())),
            mock.patch.object(turn_module, "stop", AsyncMock()),
            mock.patch.object(turn_module, "emit_changes", AsyncMock()),
            mock.patch.object(turn_module, "OpencodeDriver", driver_cls),
            mock.patch.object(
                turn_module, "time", mock.Mock(monotonic=lambda: next(clock))
            ),
        ):
            result = await turn_module.run_opencode_turn(
                request("fix the test"),
                Item(),  # type: ignore[arg-type]
                Tools(),  # type: ignore[arg-type]
                mock.Mock(),
                Limits(),  # type: ignore[arg-type]
                False,
            )
        self.assertEqual((result.state, result.reason), ("failed", "time_limit"))
        return float(driver_cls.call_args.kwargs["time_limit"])

    async def test_the_engine_start_is_counted(self) -> None:
        self.assertEqual(await self._limit(275, 20), 225)

    async def test_the_engine_limit_never_becomes_no_limit(self) -> None:
        self.assertEqual(await self._limit(25, 0), 1.0)


class AnswerLanguageTests(unittest.TestCase):
    def test_errors_follow_the_conversation_not_the_default_lang(self) -> None:
        english = request(
            "Write a function in Go that reverses a string, with unit tests, "
            "and run the tests to show they pass"
        )
        self.assertEqual(english.lang, "fr")
        self.assertEqual(english.answer_language, "en")
        self.assertEqual(request("Hallo", lang="de").answer_language, "de")
        french = request(
            "Écris une fonction en Go qui inverse une chaîne, avec des tests unitaires"
        )
        self.assertEqual(french.answer_language, "fr")

    def test_the_stream_writes_its_errors_in_the_answer_language(self) -> None:
        for name in ("chat_stream.py", "chat_api.py"):
            source = Path("services/orchestrator", name).read_text()
            self.assertNotIn("payload.lang)", source)
            self.assertNotIn("payload.lang if", source)


if __name__ == "__main__":
    unittest.main()
