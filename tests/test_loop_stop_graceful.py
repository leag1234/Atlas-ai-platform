"""A stop of the chat tool loop ends with an answer, never with its summary.

Staging -139 (2026-11-20, strict mode, owned server just started): the request
router failed twice, the model called the internal document search with the
same arguments four times in a row, the loop stopped on loop_detected, and the
person read the loop's own summary ("Explicit stop: loop_detected. Tools
executed: 2; limit 10. Tokens reserved: ..."), then "an unexpected error", then
Open WebUI's "The response was interrupted". The tests below replay that
sequence with fakes, in standard and strict modes, streamed and through the API.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
import json
import os
import re
import tempfile
import time
from typing import Any
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from packages import privacy
from packages.profiles import PROFILES
from packages.user_errors import (
    internal_diagnostic,
    stop_answer,
    without_diagnostics,
)
from services.orchestrator.chat_api import app
from services.orchestrator.chat_pipeline import NO_PASSAGE
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.closing import (
    LOOP_CLOSING_MARK,
    REPEATED_CALL_CODE,
    closing_requested,
)
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import (
    Call,
    GatewayError,
    Limits,
    Message,
    Query,
    Reservation,
    Result,
    Turn,
    run,
)
from services.orchestrator.model import GatewayCallError
from test_search_limit_closing import Scripted, gateway, offered

QUESTION = "donne moi des infos sur Orvane et Lumen Mobile"
ANSWER = (
    "Orvane est un système fictif pour téléphones. Je n'ai trouvé aucun document "
    "interne à son sujet : ces points ne sont pas vérifiés."
)
RAG = Call("r1", "rag_search", json.dumps({"query": "Orvane Lumen Mobile"}))
EMPTY: Message = {"trust": "untrusted", "data": {"passages": []}, **NO_PASSAGE}
DIAGNOSTIC = re.compile(
    r"Explicit stop|Tools executed|Tokens reserved|Cost reserved|limit None|"
    r"RuntimeError|request_stopped|stream_error|unexpected (?:failure|error)|"
    r"erreur inattendue|interrupted|interrompue",
    re.IGNORECASE,
)


def repeating(messages: list[Message]) -> tuple[str, tuple[Call, ...]]:
    """The model of -139: the same search while a tool is offered, then an answer."""
    if not offered(messages):
        return ANSWER, ()
    steps = sum(message.get("role") == "tool" for message in messages)
    return "", (Call(f"r{steps + 1}", RAG.name, RAG.arguments),)


def stubborn(messages: list[Message]) -> tuple[str, tuple[Call, ...]]:
    """A model that calls the same tool even when no tool is offered."""
    steps = sum(message.get("role") == "tool" for message in messages)
    return "", (Call(f"r{steps + 1}", RAG.name, RAG.arguments),)


class Repeating(Scripted):
    def __init__(self, reply: Callable[[list[Message]], tuple[str, tuple[Call, ...]]]):
        super().__init__([])
        self.reply = reply

    async def complete(self, messages: list[Message], timeout: float) -> Turn:
        self.seen.append(json.loads(json.dumps(messages)))
        text, calls = self.reply(messages)
        return Turn(text, calls)


class Lookup:
    """The internal document search: nothing matches, or first a timeout."""

    def __init__(self, outputs: list[Message] | None = None) -> None:
        self.outputs = list(outputs or [])
        self.executed: list[str] = []

    def estimate(self, call: Call) -> Reservation:
        return Reservation(0, Decimal(0))

    async def execute(self, call: Call, timeout: float) -> Message:
        self.executed.append(call.name)
        return dict(self.outputs.pop(0)) if self.outputs else dict(EMPTY)


async def chat_turn(
    model: Scripted,
    tools: Lookup,
    level: str = privacy.STANDARD,
    cost: Decimal = Decimal("0.05"),
) -> Result:
    token = privacy.elevate(level)
    try:
        return await run(
            Query(question=QUESTION, lang="fr"),
            model,
            tools,
            "",
            Limits(profile=PROFILES[0], cost=cost),
            final_only=True,
        )
    finally:
        privacy.reset(token)


def tool_results(messages: list[Message]) -> list[dict[str, Any]]:
    return [json.loads(str(m["content"])) for m in messages if m.get("role") == "tool"]


class GuardTests(unittest.TestCase):
    def test_stop_summaries_are_recognised_and_removed(self) -> None:
        summary = (
            "Explicit stop: loop_detected. Tools executed: 2; limit 10. Tokens "
            "reserved: 37116; limit None. Cost reserved: 0.00 EUR; limit 0.10 EUR."
        )
        failure = (
            "The request did not complete because of an unexpected failure "
            "(RuntimeError), which is not a budget or deadline refusal."
        )
        for diagnostic in (summary, failure):
            with self.subTest(diagnostic=diagnostic[:20]):
                self.assertTrue(internal_diagnostic(diagnostic))
                self.assertEqual(without_diagnostics(diagnostic), "")
                kept = without_diagnostics(ANSWER + "\n\n" + diagnostic)
                self.assertEqual(kept, ANSWER)
        self.assertFalse(internal_diagnostic(ANSWER))
        self.assertEqual(without_diagnostics(ANSWER), ANSWER)

    def test_every_research_stop_has_a_sentence_in_the_five_languages(self) -> None:
        for reason in (
            "loop_detected",
            "tool_calls",
            "search_limit",
            "fetch_limit",
            "answer_step_limit",
            "protocol_completion_limit",
            "wall_clock",
        ):
            for language in ("fr", "en", "de", "es", "it", "pt"):
                with self.subTest(reason=reason, language=language):
                    sentence = stop_answer(reason, language)
                    assert sentence is not None
                    self.assertIsNone(DIAGNOSTIC.search(sentence))
                    self.assertNotRegex(sentence, r"\bquota\b|\b[a-z]+_[a-z_]+\b")
        # Budget stops keep their own measured refusal (D5).
        self.assertIsNone(stop_answer("cost", "fr"))
        self.assertIsNone(stop_answer("tokens", "fr"))


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_same_call_again_gets_the_first_result_without_running(
        self,
    ) -> None:
        model = Scripted(
            [
                Turn("", (RAG,)),
                Turn("", (Call("r2", RAG.name, RAG.arguments),)),
                Turn(ANSWER),
            ]
        )
        tools = Lookup()
        result = await chat_turn(model, tools)
        self.assertEqual(
            (result.state, result.text, result.reason), ("done", ANSWER, "")
        )
        self.assertEqual(tools.executed, ["rag_search"])
        first, repeat = tool_results(model.seen[2])
        self.assertEqual(first, EMPTY)
        self.assertEqual(repeat["error"], REPEATED_CALL_CODE)
        self.assertEqual(repeat["previous_result"], EMPTY)
        self.assertIn("Not run again", repeat["message"])
        # The tools stay offered: the model may still change its arguments.
        self.assertTrue(offered(model.seen[2]))

    async def test_the_model_may_change_course_after_a_repeat(self) -> None:
        other = Call("r3", "calculator", json.dumps({"expression": "2+2"}))
        model = Scripted(
            [
                Turn("", (RAG,)),
                Turn("", (Call("r2", RAG.name, RAG.arguments),)),
                Turn("", (other,)),
                Turn(ANSWER),
            ]
        )
        tools = Lookup([EMPTY, {"value": "4"}])
        result = await chat_turn(model, tools)
        self.assertEqual((result.state, result.text), ("done", ANSWER))
        self.assertEqual(tools.executed, ["rag_search", "calculator"])

    async def test_a_third_identical_call_closes_research_and_the_model_answers(
        self,
    ) -> None:
        for level in (privacy.STANDARD, privacy.STRICT):
            with self.subTest(level=level):
                model = Repeating(repeating)
                tools = Lookup()
                result = await chat_turn(model, tools, level)
                self.assertEqual((result.state, result.text), ("done", ANSWER))
                self.assertEqual(result.reason, "loop_detected")
                self.assertEqual(tools.executed, ["rag_search"])
                closing = model.seen[-1]
                self.assertTrue(
                    str(closing[-1]["content"]).startswith(LOOP_CLOSING_MARK)
                )
                self.assertIn("Answer in French", str(closing[-1]["content"]))
                self.assertTrue(closing_requested(closing))
                self.assertEqual(offered(closing), [])
                # Every call of the stopped batch has its result.
                ids = {
                    call["id"]
                    for m in closing
                    for call in m.get("tool_calls") or []  # type: ignore[attr-defined]
                }
                answered = {
                    m.get("tool_call_id") for m in closing if m["role"] == "tool"
                }
                self.assertEqual(ids, answered)
                self.assertEqual(len(model.seen), 4)

    async def test_a_closing_call_that_fails_ends_with_one_honest_sentence(
        self,
    ) -> None:
        class Failing(Repeating):
            async def complete(self, messages: list[Message], timeout: float) -> Turn:
                if not offered(messages):
                    raise GatewayError("provider_error", 502, "provider_error")
                return await super().complete(messages, timeout)

        for model in (Repeating(stubborn), Failing(repeating)):
            with self.subTest(model=type(model).__name__):
                result = await chat_turn(model, Lookup(), privacy.STRICT)
                self.assertEqual(result.state, "done")
                self.assertEqual(result.text, stop_answer("loop_detected", "fr"))
                self.assertIsNone(DIAGNOSTIC.search(result.text))

    async def test_a_transient_failure_runs_once_more(self) -> None:
        tools = Lookup([{"error": "timeout"}, EMPTY])
        result = await chat_turn(Repeating(repeating), tools)
        self.assertEqual((result.state, result.text), ("done", ANSWER))
        self.assertEqual(tools.executed, ["rag_search", "rag_search"])

    async def test_a_budget_stop_keeps_its_measured_refusal(self) -> None:
        # D5: the budget refusal keeps its values; it is not turned into an answer.
        result = await chat_turn(Repeating(repeating), Lookup(), cost=Decimal("0.0015"))
        self.assertEqual((result.state, result.reason), ("stopped", "cost"))
        self.assertIn("Cost reserved", result.text)


def observed(level: str, payloads: list[dict[str, object]]) -> Any:
    """The gateway's answer for the -139 model, with what the gateway reports.

    The chat pipeline observes every call (model.observing) and runs on a
    profile, whose cost is the one the gateway measured: the real gateway
    always reports both, the call running on an owned server in strict mode.
    """
    stream = gateway(repeating, payloads)
    where = {
        "provider": "escalade" if level == privacy.STANDARD else "owned",
        "route": "simple",
    }

    async def receive(
        url: str, payload: dict[str, object], timeout: float, sink: Any
    ) -> dict[str, object]:
        wire = await stream(url, payload, timeout, sink)
        return {**wire, "observation": where, "cost_eur": "0.0001"}

    return receive


@contextmanager
def replayed_turn(level: str) -> Iterator[list[dict[str, object]]]:
    """The -139 sequence through the real chat pipeline, with fake services.

    The router's two calls fail (GatewayCallError), the internal document
    search finds nothing, and the model repeats the same search while a tool
    is offered, then answers when research is closed. A streamed turn reaches
    the model through /agent/stream, an API turn through /agent/complete.
    """
    from services.orchestrator import chat_pipeline
    from services.orchestrator.model import Configuration, GatewayModel

    payloads: list[dict[str, object]] = []
    receive = observed(level, payloads)

    async def router(payload: Message, seconds: float) -> Message:
        raise GatewayCallError("provider_error", 502)

    async def ignored(event: dict[str, object]) -> None:
        return None

    async def post(url: str, payload: Message, timeout: float) -> Message:
        if url.endswith("/agent/complete"):
            answer: Message = await receive(url, payload, timeout, ignored)
            return answer
        if url.endswith("/search"):
            return {"passages": []}
        raise AssertionError("unexpected call to " + url)

    async def connect(
        cls: object, url: str, local_enabled: bool = True
    ) -> GatewayModel:
        return GatewayModel(
            url,
            Configuration(
                input_eur_per_mtok=Decimal("0.6"),
                output_eur_per_mtok=Decimal("3.6"),
                max_tokens=2048,
            ),
        )

    with (
        tempfile.TemporaryDirectory() as directory,
        patch.dict(
            os.environ,
            {
                "ATLAS_INTERACTION_DIR": directory,
                "ATLAS_MODEL_ROUTER": "1",
                "ATLAS_PRIVACY_MINIMUM": "" if level == privacy.STANDARD else level,
            },
        ),
        patch.object(chat_pipeline, "dispatch_project", AsyncMock(return_value=False)),
        patch(
            "services.orchestrator.user_memory.prepare", AsyncMock(return_value=False)
        ),
        patch(
            "services.orchestrator.chat_work.escalate_to_work",
            AsyncMock(return_value=False),
        ),
        patch("services.orchestrator.request_router._gateway", router),
        patch.object(GatewayModel, "connect", classmethod(connect)),
        patch.object(GatewayModel, "post", staticmethod(post)),
        patch("services.orchestrator.model.receive", new=receive),
    ):
        yield payloads


def request_body(level: str, **extra: object) -> dict[str, object]:
    return {
        "ui_locale": "fr",
        "messages": [{"role": "user", "content": QUESTION}],
        **extra,
    }


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def streamed(
        self, level: str, atlas_ui: bool
    ) -> tuple[str, list[dict[str, Any]], Interaction]:
        from services.orchestrator.chat_pipeline import process

        payload = ChatRequest.model_validate(
            request_body(level, stream=True, atlas_ui=atlas_ui)
        )
        item = Interaction()
        token = privacy.elevate(level)
        try:
            with replayed_turn(level) as payloads:
                rows = [
                    json.loads(str(row).removeprefix("data: "))
                    async for row in response(
                        payload, item, time.monotonic(), process
                    ).body_iterator
                    if str(row).startswith("data: {")
                ]
        finally:
            privacy.reset(token)
        self.assertGreaterEqual(len(payloads), 4)
        text = "".join(
            str(row.get("choices", [{}])[0].get("delta", {}).get("content") or "")
            for row in rows
        )
        return text, rows, item

    async def test_the_139_sequence_streams_a_finished_answer(self) -> None:
        for level in (privacy.STANDARD, privacy.STRICT):
            for atlas_ui in (True, False):
                with self.subTest(level=level, atlas_ui=atlas_ui):
                    text, rows, item = await self.streamed(level, atlas_ui)
                    self.assertFalse(any("error" in row for row in rows), rows[-3:])
                    self.assertIn(ANSWER, text)
                    self.assertIsNone(DIAGNOSTIC.search(text))
                    self.assertEqual(rows[-1]["choices"][0]["finish_reason"], "stop")
                    self.assertEqual(item.state, "done")
                    self.assertIn("loop_detected", item.erreurs)

    async def test_a_stopped_turn_streams_its_sentence_not_its_summary(self) -> None:
        summary = (
            "Explicit stop: loop_detected. Tools executed: 2; limit 10. Tokens "
            "reserved: 37116; limit None. Cost reserved: 0.00 EUR; limit 0.10 EUR."
        )

        async def process(payload: ChatRequest, item: Interaction) -> None:
            item.state, item.reponse = "stopped", summary
            item.erreurs.append("loop_detected")

        payload = ChatRequest.model_validate(
            request_body(privacy.STRICT, stream=True, atlas_ui=True)
        )
        item = Interaction()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
        ):
            wire = "".join(
                [
                    str(chunk)
                    async for chunk in response(
                        payload, item, time.monotonic(), process
                    ).body_iterator
                ]
            )
        self.assertIn(json.dumps(str(stop_answer("loop_detected", "fr")))[1:30], wire)
        self.assertIsNone(DIAGNOSTIC.search(wire))
        self.assertNotIn('"error"', wire)


class ApiTests(unittest.TestCase):
    def test_the_139_sequence_answers_through_the_api(self) -> None:
        for level in (privacy.STANDARD, privacy.STRICT):
            with self.subTest(level=level):
                with replayed_turn(level) as payloads, TestClient(app) as client:
                    answer = client.post(
                        "/v1/chat/completions", json=request_body(level)
                    )
                self.assertEqual(answer.status_code, 200, answer.text[:300])
                content = answer.json()["choices"][0]["message"]["content"]
                self.assertIn(ANSWER, content)
                self.assertIsNone(DIAGNOSTIC.search(content))
                self.assertGreaterEqual(len(payloads), 4)


if __name__ == "__main__":
    unittest.main()
