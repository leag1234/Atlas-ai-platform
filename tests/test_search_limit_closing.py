"""A search refused for a limit is followed by one closing call: the answer."""

from collections.abc import Awaitable, Callable
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Literal
import unittest
from unittest.mock import AsyncMock, patch

from packages.profiles import PROFILES
from services.orchestrator.chat_pipeline import ChatTools
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.closing import (
    CLOSING_MARK,
    SEARCH_LIMITS,
    closing_instruction,
    closing_requested,
    limit_code,
)
from services.orchestrator.code_mode import code_profile
from services.orchestrator.daily_budget import DailyLedger
from services.orchestrator.diagram import DeliveryBuffer
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import (
    Call,
    Limits,
    Message,
    Query,
    Reservation,
    Result,
    Turn,
    run,
)
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.stream_client import Sink, sink_context
from services.orchestrator.tools import Runtime, SearchQuotaError

QUESTION = "what's the latest news on the Harbour Act? include sources"
NOTE = "The first results are off topic. I will run a more precise search."
PLAN = (
    "The internal search did not return any document about the Harbour Act. "
    "Since I have hit the search limit, I must rely on my training data.\n\n"
    "**Plan:**\n1. Acknowledge the search limit.\n2. Give the timeline.\n\n"
    "I will write the response now."
)
ANSWER = (
    "Web search was not available because its limit is reached, so this may be "
    "dated. The fictional Harbour Act applies in three stages."
)
REFUSAL: Message = {
    "error": "daily_search_limit",
    "message": "search limit reached today (21 of 20)",
}
FOUND: Message = {
    "trust": "untrusted",
    "data": {
        "results": [
            {
                "title": "Harbour Act",
                "link": "https://example.org/harbour-act",
                "snippet": "Three stages.",
            }
        ]
    },
}


def search(identifier: str, tool: str = "web_search") -> Call:
    return Call(
        identifier,
        tool,
        json.dumps({"query": "Harbour Act " + identifier, "lang": "en"}),
    )


def gateway_model(sink: Sink | None = None) -> GatewayModel:
    built = GatewayModel(
        "http://127.0.0.1:1",
        Configuration(
            input_eur_per_mtok=Decimal("0.6"),
            output_eur_per_mtok=Decimal("3.6"),
            max_tokens=2048,
        ),
    )
    if sink is not None:
        # A research turn: no turn is streamed before it is known to be the answer.
        built.sink, built.note_hold = sink, "turn"
    return built


def offered(messages: list[Message]) -> list[str]:
    """The tools the chat model would be offered for this call."""
    return [
        str(function["name"])
        for tool in gateway_model().available_tools(messages)
        if isinstance(function := tool.get("function"), dict)
    ]


class Research:
    """Searches answer until the allowance is used up; other tools always answer."""

    def __init__(self, allowance: int = 0, refusal: Message = REFUSAL) -> None:
        self.allowance, self.refusal = allowance, refusal
        self.executed: list[str] = []

    def estimate(self, call: Call) -> Reservation:
        return Reservation(0, Decimal(0))

    async def execute(self, call: Call, timeout: float) -> Message:
        sink = sink_context.get()
        if sink:
            await sink({"phase": "tool_started", "tool": call.name, "urls": []})
        self.executed.append(call.name)
        if call.name in {"web_search", "web_images"}:
            self.allowance -= 1
            if self.allowance < 0:
                # As in the chat tools: a refusal ends the step without a result.
                return dict(self.refusal)
        if sink:
            await sink({"phase": "tool_finished", "tool": call.name, "urls": []})
        return dict(FOUND) if call.name == "web_search" else {"value": "2"}


class Scripted:
    """Plays its turns in order, whatever it is sent."""

    def __init__(self, turns: list[Turn], cost: str = "0.001") -> None:
        self.turns, self.cost = list(turns), Decimal(cost)
        self.seen: list[list[Message]] = []
        self.before: Callable[[], None] | None = None

    def estimate(self, messages: list[Message]) -> Reservation:
        if self.before is not None:
            self.before()
        return Reservation(10, self.cost)

    async def complete(self, messages: list[Message], timeout: float) -> Turn:
        self.seen.append(json.loads(json.dumps(messages)))
        return self.turns.pop(0)


def wandering(messages: list[Message]) -> tuple[str, tuple[Call, ...]]:
    """The model of the defect: it keeps working while it is offered a tool.

    It searches, turns to internal documents when the search is refused, then
    ends on its planning notes. Told to close, with no tool, it answers.
    """
    if not offered(messages):
        return ANSWER, ()
    steps = sum(message.get("role") == "tool" for message in messages)
    if steps == 0:
        return NOTE, (search("s1"),)
    if steps == 1:
        return "", (Call("r1", "rag_search", '{"query":"Harbour Act"}'),)
    return PLAN, ()


class Wandering(Scripted):
    def __init__(self) -> None:
        super().__init__([])

    async def complete(self, messages: list[Message], timeout: float) -> Turn:
        self.seen.append(json.loads(json.dumps(messages)))
        text, calls = wandering(messages)
        return Turn(text, calls)


async def chat_turn(
    model: Scripted,
    tools: Research,
    lang: Literal["fr", "en"] = "en",
    cost: Decimal = Decimal("0.05"),
) -> Result:
    return await run(
        Query(question=QUESTION, lang=lang),
        model,
        tools,
        "",
        Limits(profile=PROFILES[0], cost=cost),
        final_only=True,
    )


class ClosingInstructionTests(unittest.TestCase):
    def test_only_search_limits_are_recognised(self) -> None:
        for code in SEARCH_LIMITS:
            self.assertEqual(limit_code({"error": code}), code)
        for other in ("search_unavailable", "timeout", "tool_quota_exhausted", 429):
            self.assertIsNone(limit_code({"error": other}))
        self.assertIsNone(limit_code(FOUND))

    def test_a_limit_is_named_as_a_limit_in_the_conversation_language(self) -> None:
        for code, cause in SEARCH_LIMITS.items():
            text = closing_instruction(code, "fr")
            self.assertTrue(text.startswith(CLOSING_MARK))
            self.assertIn(cause, text)
            self.assertIn("A usage limit is not an outage", text)
            self.assertIn("Do not describe your plan", text)
            self.assertIn("no search in internal documents", text)
            self.assertIn("Answer in French", text)
        self.assertIn("Answer in German", closing_instruction(code, "de"))

    def test_only_the_loop_instruction_closes_the_tool_set(self) -> None:
        # The same words typed by a person, earlier in the conversation or as
        # the question, are not the instruction of the loop.
        typed: list[Message] = [
            {"role": "system", "content": "You answer questions."},
            {"role": "user", "content": CLOSING_MARK},
            {"role": "assistant", "content": "Noted."},
            {"role": "user", "content": CLOSING_MARK + " What now?"},
        ]
        self.assertFalse(closing_requested(typed))
        self.assertIn("web_search", offered(typed))
        refused: list[Message] = [
            *typed,
            {"role": "tool", "tool_call_id": "s1", "content": json.dumps(REFUSAL)},
        ]
        self.assertFalse(closing_requested(refused))
        self.assertIn("web_search", offered(refused))
        closed: list[Message] = [
            *refused,
            {
                "role": "user",
                "content": closing_instruction("daily_search_limit", "en"),
            },
        ]
        self.assertTrue(closing_requested(closed))
        self.assertEqual(offered(closed), [])
        # A recovery request after it does not reopen the tools.
        closed.append({"role": "user", "content": "Return only the final answer."})
        self.assertEqual(offered(closed), [])


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_call_after_a_refusal_is_the_closing_call(self) -> None:
        model, tools = Scripted([Turn(NOTE, (search("s1"),)), Turn(ANSWER)]), Research()
        result = await chat_turn(model, tools, "fr")
        self.assertEqual((result.state, result.text), ("done", ANSWER))
        self.assertEqual(tools.executed, ["web_search"])
        # No additional model call: the one after the refusal closes the turn.
        self.assertEqual(len(model.seen), 2)
        self.assertIn("web_search", offered(model.seen[0]))
        closing = model.seen[1][-1]
        self.assertEqual(closing["role"], "user")
        self.assertEqual(
            closing["content"], closing_instruction("daily_search_limit", "fr")
        )
        self.assertEqual(offered(model.seen[1]), [])
        # The refusal itself stays in front of the model, with its measures.
        self.assertIn("21 of 20", str(model.seen[1][-2]["content"]))

    async def test_planning_notes_after_a_refusal_are_never_the_answer(self) -> None:
        model, tools = Wandering(), Research()
        result = await chat_turn(model, tools)
        self.assertEqual((result.state, result.text), ("done", ANSWER))
        self.assertNotIn("Plan", result.text)
        # Internal documents did not stand in for the web.
        self.assertEqual(tools.executed, ["web_search"])
        self.assertEqual(len(model.seen), 2)

    async def test_results_obtained_before_the_refusal_are_kept(self) -> None:
        model = Scripted(
            [Turn("", (search("s1"),)), Turn(NOTE, (search("s2"),)), Turn(ANSWER)]
        )
        tools = Research(allowance=1)
        result = await chat_turn(model, tools)
        self.assertEqual((result.state, result.text), ("done", ANSWER))
        self.assertEqual(len(model.seen), 3)
        self.assertIn("web_search", offered(model.seen[1]))
        self.assertEqual(offered(model.seen[2]), [])
        self.assertIn("https://example.org/harbour-act", json.dumps(model.seen[2]))

    async def test_the_rest_of_the_batch_runs_without_another_search(self) -> None:
        batch = (
            search("s1"),
            search("s2"),
            Call("c1", "calculator", '{"expr":"1+1"}'),
            search("i1", "web_images"),
        )
        model, tools = Scripted([Turn("", batch), Turn(ANSWER)]), Research()
        result = await chat_turn(model, tools)
        self.assertEqual(result.text, ANSWER)
        self.assertEqual(tools.executed, ["web_search", "calculator"])
        self.assertEqual(result.tool_calls, 2)
        results = {
            str(message["tool_call_id"]): json.loads(str(message["content"]))
            for message in model.seen[1]
            if message.get("role") == "tool"
        }
        self.assertEqual(set(results), {"s1", "s2", "c1", "i1"})
        self.assertEqual(results["s2"], REFUSAL)
        self.assertEqual(results["i1"], REFUSAL)
        self.assertEqual(results["c1"], {"value": "2"})
        # One instruction, after every result of the batch.
        marks = [
            m for m in model.seen[1] if str(m.get("content")).startswith(CLOSING_MARK)
        ]
        self.assertEqual(marks, [model.seen[1][-1]])

    async def test_a_tool_called_in_the_closing_turn_is_never_run(self) -> None:
        model = Scripted([Turn("", (search("s1"),)), Turn("", (search("s2"),))])
        tools = Research()
        result = await chat_turn(model, tools)
        self.assertEqual(
            (result.state, result.reason), ("stopped", "tool_quota_exhausted")
        )
        self.assertEqual(tools.executed, ["web_search"])
        self.assertEqual(len(model.seen), 2)

    async def test_every_search_limit_closes_and_an_outage_does_not(self) -> None:
        for code in SEARCH_LIMITS:
            with self.subTest(code=code):
                model = Scripted([Turn("", (search("s1"),)), Turn(ANSWER)])
                result = await chat_turn(
                    model, Research(refusal={"error": code, "provider_status": 429})
                )
                self.assertEqual(result.text, ANSWER)
                self.assertEqual(
                    model.seen[1][-1]["content"], closing_instruction(code, "en")
                )
        for outage in ("search_unavailable", "timeout"):
            with self.subTest(outage=outage):
                model = Scripted([Turn("", (search("s1"),)), Turn(ANSWER)])
                await chat_turn(model, Research(refusal={"error": outage}))
                self.assertFalse(closing_requested(model.seen[1]))
                self.assertIn("web_search", offered(model.seen[1]))

    async def test_a_turn_without_refusal_is_unchanged(self) -> None:
        model = Scripted([Turn(NOTE, (search("s1"),)), Turn(ANSWER)])
        tools = Research(allowance=3)
        result = await chat_turn(model, tools)
        self.assertEqual((result.state, result.text), ("done", ANSWER))
        self.assertEqual(len(model.seen), 2)
        self.assertEqual(
            [m["role"] for m in model.seen[1]], ["system", "user", "assistant", "tool"]
        )
        self.assertFalse(closing_requested(model.seen[1]))
        self.assertIn("web_search", offered(model.seen[1]))

    async def test_the_closing_call_is_reserved_like_any_other(self) -> None:
        model = Scripted([Turn("", (search("s1"),)), Turn(ANSWER)], cost="0.03")
        result = await chat_turn(model, Research(), cost=Decimal("0.05"))
        self.assertEqual((result.state, result.reason), ("stopped", "cost"))
        self.assertEqual(len(model.seen), 1)

    async def test_the_closing_call_keeps_the_deadline(self) -> None:
        now = [0.0]
        model, tools = Scripted([Turn("", (search("s1"),)), Turn(ANSWER)]), Research()
        result = await run(
            Query(question=QUESTION, lang="en"),
            model,
            tools,
            "",
            Limits(profile=PROFILES[0], wall_clock=100),
            clock=lambda: now[0],
            final_only=True,
        )
        self.assertEqual(result.text, ANSWER)
        # The same turn when no time is left once the search is refused.
        model, tools = Scripted([Turn("", (search("s1"),)), Turn(ANSWER)]), Research()

        def late() -> None:
            if model.seen:
                now[0] = 500.0

        model.before = late
        result = await run(
            Query(question=QUESTION, lang="en"),
            model,
            tools,
            "",
            Limits(profile=PROFILES[0], wall_clock=100),
            clock=lambda: now[0],
            final_only=True,
        )
        self.assertEqual((result.state, result.reason), ("stopped", "wall_clock"))
        self.assertEqual(len(model.seen), 1)

    async def test_a_used_up_allowance_in_the_chat_tools_closes_the_turn(self) -> None:
        model = Scripted([Turn("", (search("s1"),)), Turn(ANSWER)])
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_WEB_CACHE": root + "/cache",
                    "ATLAS_SEARCH_PROVIDER": "tavily",
                    "TAVILY_API_KEY": "fixture",
                    "ATLAS_USER_DAILY_SEARCHES": "1",
                },
            ),
        ):
            tools = ChatTools(Interaction())
            tools.daily_ledger = DailyLedger(Path(root) / "ledger")
            tools.daily_ledger.reserve_search("unknown", "en", "tavily")
            result = await run(
                Query(question=QUESTION, lang="en"),
                model,
                tools,
                "",
                Limits(profile=PROFILES[0]),
                final_only=True,
            )
        self.assertEqual((result.state, result.text), ("done", ANSWER))
        refusal = json.loads(str(model.seen[1][-2]["content"]))
        self.assertEqual(refusal["error"], "daily_search_limit")
        self.assertIn("2 of 1", refusal["message"])
        self.assertTrue(closing_requested(model.seen[1]))
        self.assertEqual(offered(model.seen[1]), [])

    async def test_a_quota_refusal_after_served_results_keeps_the_answer(
        self,
    ) -> None:
        # Staging 2026-11-14: the first search was served by the local engine,
        # the second reached an exhausted provider, and the sourced answer was
        # refused with 503. The refusal now closes the research instead.
        served = {
            "provider": "searxng",
            "outcome": "partial_results",
            "results": [
                {
                    "title": "Harbour Act",
                    "link": "https://example.org/harbour-act",
                    "snippet": "Three stages.",
                }
            ],
        }
        empty = {"provider": "searxng", "outcome": "empty_results", "results": []}
        model = Scripted(
            [Turn("", (search("s1"),)), Turn("", (search("s2"),)), Turn(ANSWER)]
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_WEB_CACHE": root + "/cache",
                    "ATLAS_SEARCH_PROVIDER": "searxng,tavily",
                    "TAVILY_API_KEY": "fixture",
                },
            ),
            patch.object(Runtime, "searxng", AsyncMock(side_effect=[served, empty])),
            patch.object(
                Runtime, "tavily", AsyncMock(side_effect=SearchQuotaError(432))
            ),
        ):
            item = Interaction()
            result = await run(
                Query(question=QUESTION, lang="en"),
                model,
                ChatTools(item),
                "",
                Limits(profile=PROFILES[0]),
                final_only=True,
            )
        self.assertEqual((result.state, result.text), ("done", ANSWER))
        refusal = json.loads(str(model.seen[2][-2]["content"]))
        self.assertEqual(
            (refusal["error"], refusal["provider_status"]), ("quota_exceeded", 432)
        )
        self.assertTrue(closing_requested(model.seen[2]))
        self.assertEqual(item.retrieved_urls, ["https://example.org/harbour-act"])

    async def test_work_keeps_its_tools_after_a_refused_search(self) -> None:
        model = Scripted([Turn("", (search("s1"),)), Turn(ANSWER)])
        with patch.dict(os.environ, {"ATLAS_PROJECT_MODE": "1"}):
            result = await run(
                Query(question=QUESTION, lang="en"),
                model,
                Research(),
                "",
                Limits(
                    profile=code_profile(),
                    tokens=None,
                    code_mode=True,
                    tool_calls=40,
                    model_calls=40,
                    cost=Decimal(1),
                ),
            )
        self.assertEqual(result.text, ANSWER)
        self.assertFalse(closing_requested(model.seen[1]))
        self.assertIn("web_search", offered(model.seen[1]))


END_OF_STREAM = {"marker": "end_of_stream"}
Receive = Callable[[str, dict[str, object], float, Sink], Awaitable[dict[str, object]]]


def gateway(
    reply: Callable[[list[Message]], tuple[str, tuple[Call, ...]]],
    payloads: list[dict[str, object]],
    seen: list[object] | None = None,
) -> Receive:
    """A gateway stream: the turn's text in small pieces, then its result."""

    async def receive(
        url: str, payload: dict[str, object], timeout: float, sink: Sink
    ) -> dict[str, object]:
        payloads.append(json.loads(json.dumps(payload)))
        messages = payload["messages"]
        assert isinstance(messages, list)
        text, calls = reply(messages)
        for start in range(0, len(text), 7):
            await sink({"delta": {"content": text[start : start + 7]}})
        if seen is not None:
            seen.append(END_OF_STREAM)
        return {
            "text": text,
            "calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in calls
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 20},
        }

    return receive


def shown(events: list[object]) -> str:
    return "".join(
        str(event["delta"].get("content", ""))
        for event in events
        if isinstance(event, dict) and isinstance(event.get("delta"), dict)
    )


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_closing_call_offers_no_tool_and_streams_live(self) -> None:
        events: list[object] = []
        payloads: list[dict[str, object]] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        messages: list[Message] = [
            {"role": "user", "content": QUESTION},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "s1",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "s1", "content": json.dumps(REFUSAL)},
            {
                "role": "user",
                "content": closing_instruction("daily_search_limit", "en"),
            },
        ]
        with patch(
            "services.orchestrator.model.receive",
            new=gateway(lambda _: (ANSWER, ()), payloads, events),
        ):
            turn = await gateway_model(sink).complete(messages, 100)
        self.assertEqual(turn.text, ANSWER)
        self.assertEqual(payloads[0]["tools"], [])
        # With no tool the turn can only be the answer: nothing is held back.
        self.assertEqual(shown(events[: events.index(END_OF_STREAM)]), ANSWER)


class TranscriptTests(unittest.IsolatedAsyncioTestCase):
    async def transcript(
        self,
        reply: Callable[[list[Message]], tuple[str, tuple[Call, ...]]],
        tools: Research,
    ) -> tuple[str, list[dict[str, object]], Interaction]:
        payloads: list[dict[str, object]] = []

        async def process(payload: ChatRequest, item: Interaction) -> None:
            sink = sink_context.get()
            assert sink is not None
            buffer = DeliveryBuffer(sink)
            result = await run(
                Query(question=QUESTION, lang="en"),
                gateway_model(buffer),
                tools,
                "",
                Limits(profile=PROFILES[0], tokens=None),
                final_only=True,
            )
            item.reponse, item.state = result.text, result.state
            await buffer.finish(item.reponse)

        payload = ChatRequest.model_validate(
            {
                "atlas_ui": True,
                "stream": True,
                "ui_locale": "en",
                "messages": [{"role": "user", "content": QUESTION}],
            }
        )
        item = Interaction()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
            patch("services.orchestrator.model.receive", new=gateway(reply, payloads)),
        ):
            rows = [
                json.loads(str(row).removeprefix("data: "))
                async for row in response(
                    payload, item, time.monotonic(), process
                ).body_iterator
                if str(row).startswith("data: {")
            ]
        text = "".join(
            row.get("choices", [{}])[0].get("delta", {}).get("content", "")
            for row in rows
        )
        return text, payloads, item

    async def test_a_refused_search_ends_on_the_answer_not_on_notes(self) -> None:
        tools = Research()
        text, payloads, item = await self.transcript(wandering, tools)
        self.assertEqual((item.state, item.reponse), ("done", ANSWER))
        self.assertEqual(text.count(ANSWER), 1)
        self.assertTrue(text.rstrip().endswith(ANSWER))
        for hidden in (NOTE[:20], "Plan", "internal search", "I will write"):
            self.assertNotIn(hidden, text)
        # Two model calls: the search request, then the closing call.
        self.assertEqual(len(payloads), 2)
        self.assertTrue(payloads[0]["tools"])
        self.assertEqual(payloads[1]["tools"], [])
        messages = payloads[1]["messages"]
        assert isinstance(messages, list)
        self.assertTrue(closing_requested(messages))
        self.assertTrue(str(messages[-1]["content"]).startswith(CLOSING_MARK))
        self.assertEqual(tools.executed, ["web_search"])

    async def test_a_research_turn_without_refusal_is_unchanged(self) -> None:
        tools = Research(allowance=3)
        text, payloads, item = await self.transcript(wandering, tools)
        # Nothing closes this turn: the model goes on as before, to its last turn.
        self.assertEqual(tools.executed, ["web_search", "rag_search"])
        self.assertEqual(len(payloads), 3)
        for payload in payloads:
            self.assertTrue(payload["tools"])
            messages = payload["messages"]
            assert isinstance(messages, list)
            self.assertFalse(closing_requested(messages))
        self.assertEqual(item.reponse, PLAN)
        self.assertTrue(text.rstrip().endswith(PLAN))
        self.assertNotIn(NOTE[:20], text)


if __name__ == "__main__":
    unittest.main()


class LimitCaveatTests(unittest.IsolatedAsyncioTestCase):
    """A turn closed for a search limit gets no contradicting "partial results" caveat."""

    async def run_request(self, closed: str) -> Interaction:
        from services.orchestrator import chat_pipeline
        from services.orchestrator.closing import LIMIT_CLOSED

        async def fake_process(request: ChatRequest, item: Interaction) -> None:
            # What the chat loop leaves: an earlier search came back partial, then
            # (or not) a later search was refused for a limit and closed the turn.
            item.state, item.reponse = "done", ANSWER
            item.erreurs.append("partial_search_failure:partial_results")
            if closed:
                LIMIT_CLOSED.set(closed)

        request = ChatRequest.model_validate(
            {"ui_locale": "en", "messages": [{"role": "user", "content": QUESTION}]}
        )
        item = Interaction()
        with (
            patch.object(
                chat_pipeline, "dispatch_project", AsyncMock(return_value=False)
            ),
            patch(
                "services.orchestrator.user_memory.prepare",
                AsyncMock(return_value=False),
            ),
            patch(
                "services.orchestrator.chat_work.escalate_to_work",
                AsyncMock(return_value=False),
            ),
            patch("services.orchestrator.request_router.enabled", return_value=False),
            patch.object(chat_pipeline, "_process", fake_process),
        ):
            await chat_pipeline._process_request(request, item)
        return item

    async def test_no_partial_results_caveat_after_a_limit_closed_the_turn(
        self,
    ) -> None:
        item = await self.run_request("daily_search_limit")
        self.assertEqual(item.reponse, ANSWER)

    async def test_the_caveat_stays_when_no_limit_closed_the_turn(self) -> None:
        item = await self.run_request("")
        self.assertTrue(item.reponse.startswith(ANSWER))
        self.assertIn("some sources were unavailable", item.reponse)
        self.assertNotIn("partial_results", item.reponse)

    async def test_the_loop_marks_the_turn_it_closed(self) -> None:
        from services.orchestrator.closing import LIMIT_CLOSED

        LIMIT_CLOSED.set("")
        model = Scripted([Turn(NOTE, (search("s1"),)), Turn(ANSWER)])
        await chat_turn(model, Research())
        self.assertEqual(LIMIT_CLOSED.get(), "daily_search_limit")
