"""POC-P6: reserve worst-case usage before starting cancellable I/O."""

from services.orchestrator.closing import (
    LIMIT_CLOSED,
    SEARCH_TOOLS,
    WRITING_RESERVE_SECONDS,
    closing_instruction,
    limit_code,
    time_closing_instruction,
)
from services.orchestrator.code_verification import VerificationStatus
from services.orchestrator.tool_failures import (
    SAME_FAILURE_STOP,
    STOP_REASON,
    RepeatedFailures,
)

from packages.profiles import accepted_profile
from packages.limits import LimitError

import asyncio
import json
import logging
import os
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from packages.validation import describe_validation

from services.guardrails.input_filter import validate_input


class Query(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, pattern=r"\S")
    lang: Literal["fr", "de", "es", "it", "en"]

    _safe_question = field_validator("question")(validate_input)


@dataclass(frozen=True)
class Limits:
    tokens: int | None = 16384
    profile: str | None = None
    tool_calls: int = 10
    wall_clock: float = 120
    cost: Decimal = Decimal("0.05")
    has_attachments: bool = False
    produces_files: bool = False
    code_mode: bool = False
    model_calls: int | None = None

    def __post_init__(self) -> None:
        if self.code_mode:
            from services.orchestrator.code_workspace import work_enabled

            if not work_enabled() or not (
                self.tokens is None
                and self.profile is not None
                and accepted_profile(self.profile)
                and 0 <= self.tool_calls <= 1000
                and 0 <= self.wall_clock <= 86400
                and self.cost.is_finite()
                and 0 <= self.cost <= 100
                and self.model_calls is not None
                and 0 <= self.model_calls <= 1001
            ):
                raise ValueError("invalid_code_limits")
            return
        if self.profile is not None and not accepted_profile(self.profile):
            raise ValueError("invalid_limits")
        if not (
            (
                (self.tokens is None and self.profile is not None)
                or (
                    self.tokens is not None
                    and 0 <= self.tokens <= (262144 if self.profile else 16384)
                )
            )
            and 0 <= self.tool_calls <= 10
            and (
                0
                <= self.wall_clock
                <= (
                    300
                    if (self.has_attachments or self.produces_files) and self.profile
                    else 120
                )
            )
            and (
                0
                <= self.cost
                <= Decimal(
                    "0.30"
                    if (self.has_attachments or self.produces_files)
                    and self.profile is not None
                    else "0.10"
                    if self.profile is not None
                    else "0.05"
                )
            )
        ):
            raise ValueError("invalid_limits")


@dataclass(frozen=True)
class Reservation:
    tokens: int
    cost: Decimal

    def __post_init__(self) -> None:
        if self.tokens < 0 or not self.cost.is_finite() or self.cost < 0:
            raise ValueError("invalid_reservation")


@dataclass(frozen=True)
class Call:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Turn:
    text: str
    calls: tuple[Call, ...] = ()
    usage: Reservation | None = None


@dataclass
class Result:
    text: str = ""
    state: str = "model"
    reason: str = ""
    tokens: int = 0
    cost: Decimal = Decimal(0)
    tool_calls: int = 0
    trace: list[dict[str, object]] = field(default_factory=list)


@dataclass
class CodeResult(Result):
    verification: VerificationStatus = field(default_factory=VerificationStatus)
    model_calls: int = 0
    limit_stop: dict[str, object] = field(default_factory=dict)


Message = dict[str, object]


class Model(Protocol):
    def estimate(self, messages: list[Message]) -> Reservation: ...
    async def complete(self, messages: list[Message], timeout: float) -> Turn: ...


class Tools(Protocol):
    def estimate(self, call: Call) -> Reservation: ...
    async def execute(self, call: Call, timeout: float) -> Message: ...


class ResultReferences(Protocol):
    async def retain(
        self, call: Call, output: Message, messages: list[Message]
    ) -> None: ...
    async def reopen(self, call: Call) -> Message: ...


class Stop(Exception):
    pass


class PublicFailure(RuntimeError):
    """A typed boundary failure whose user-facing detail must survive the loop."""


class GatewayError(PublicFailure):
    def __init__(
        self,
        code: str,
        status: int,
        detail: str = "",
        retries: int = 0,
        measurements: dict[str, object] | None = None,
    ) -> None:
        self.code, self.status, self.detail = code, status, detail
        self.retries = retries
        self.measurements = measurements or {}
        super().__init__(code)


async def run(
    query: Query,
    model: Model,
    tools: Tools,
    system: str,
    limits: Limits = Limits(),
    clock: Callable[[], float] = time.monotonic,
    history: list[Message] | None = None,
    retry_web: bool = False,
    retry_protocol: bool = True,
    terminal_tools: frozenset[str] = frozenset(),
    initial_calls: tuple[Call, ...] = (),
    references: ResultReferences | None = None,
    skill_messages: list[Message] | None = None,
    include_intermediate: bool = True,
    final_only: bool = False,
    before_final: Callable[[list[str]], str | None] | None = None,
) -> Result:
    result = CodeResult() if limits.code_mode else Result()
    deadline = clock() + limits.wall_clock
    messages: list[Message] = [
        {"role": "system", "content": system},
        *(history or []),
        {
            "role": "user",
            "content": json.dumps(
                {
                    **(
                        {"scope": Path("prompts/current-question.txt").read_text()}
                        if limits.profile
                        else {}
                    ),
                    **query.model_dump(),
                },
                ensure_ascii=False,
            ),
        },
    ]
    counts: Counter[str] = Counter()
    recent: list[tuple[str, str]] = []
    nudged: set[tuple[str, str]] = set()
    quota_recovered = False
    quota_denials = 0
    # Chat only: the first search refused for a limit, and whether the model
    # was told that its next call closes the turn (see closing.py).
    closes_on_limit = bool(limits.profile) and not limits.code_mode
    last_call_started = 0.0
    # Once research has started, a chat turn keeps this much time for writing
    # its answer; never more than a third of a short turn.
    writing_reserve = min(
        float(
            os.environ.get(
                "ATLAS_CHAT_WRITING_RESERVE_SECONDS", WRITING_RESERVE_SECONDS
            )
        ),
        limits.wall_clock / 3,
    )
    limit_refusal: Message | None = None
    closed = False
    protocol_recovered = False
    links_recovered = False
    retrieved: set[str] = set()
    tool_urls: set[str] = set()
    answer_parts: list[str] = []
    last_plan: tuple[str, Message] | None = None
    previous_call: tuple[str, str] | None = None
    skills_inserted = 0
    # Work only: the same tool failing the same way, whatever its arguments.
    failures = RepeatedFailures() if limits.code_mode else None

    def record_limit(values: dict[str, object]) -> None:
        if isinstance(result, CodeResult):
            result.limit_stop = values

    def remaining() -> float:
        seconds = deadline - clock()
        if seconds <= 0:
            record_limit(
                {
                    "kind": "wall_clock",
                    "measured": clock() - (deadline - limits.wall_clock),
                    "limit": limits.wall_clock,
                    "unit": "seconds",
                }
            )
            raise Stop("wall_clock")
        return seconds

    def reserve(usage: Reservation) -> None:
        remaining()
        if limits.tokens is not None and result.tokens + usage.tokens > limits.tokens:
            raise Stop("tokens")
        if result.cost + usage.cost > limits.cost:
            record_limit(
                {
                    "kind": "cost",
                    "measured": str(result.cost + usage.cost),
                    "limit": str(limits.cost),
                    "unit": "EUR",
                }
            )
            raise Stop("cost")
        result.tokens += usage.tokens
        result.cost += usage.cost

    async def invoke(operation: Awaitable[Turn] | Awaitable[Message]) -> Turn | Message:
        try:
            async with asyncio.timeout(remaining()):
                value = await operation
            remaining()
            return value
        except TimeoutError:
            record_limit(
                {
                    "kind": "wall_clock",
                    "measured": clock() - (deadline - limits.wall_clock),
                    "limit": limits.wall_clock,
                    "unit": "seconds",
                }
            )
            raise Stop("wall_clock") from None

    try:
        while True:
            # Only the catalog loader supplies this separate instruction channel.
            # Insert after every tool in a batch has its response, and keep the
            # messages in this run's context without persisting them as history.
            if skill_messages is not None:
                messages.extend(dict(m) for m in skill_messages[skills_inserted:])
                skills_inserted = len(skill_messages)
            if initial_calls:
                turn = Turn("", initial_calls, Reservation(0, Decimal(0)))
                initial_calls = ()
            else:
                result.state = "model"
                if (
                    isinstance(result, CodeResult)
                    and limits.model_calls is not None
                    and result.model_calls >= limits.model_calls
                ):
                    record_limit(
                        {
                            "kind": "model_calls",
                            "measured": result.model_calls,
                            "limit": limits.model_calls,
                            "unit": "calls",
                        }
                    )
                    raise Stop("model_calls")
                if limit_refusal is not None and not closed:
                    # After every pending tool result, and before the one model
                    # call that follows: it is offered no tool and writes the
                    # answer. Reserved and timed below like any other call.
                    # The instruction is the last message the model reads: it
                    # was followed to the letter there, and only in part as a
                    # system message placed before the repeated question.
                    closed = True
                    LIMIT_CLOSED.set(str(limit_refusal["error"]))
                    messages.append(
                        {
                            "role": "user",
                            "content": closing_instruction(
                                str(limit_refusal["error"]), query.lang
                            ),
                        }
                    )
                    logging.getLogger(__name__).info(
                        json.dumps(
                            {
                                "event": "search_limit_closing",
                                "limit": limit_refusal["error"],
                                "tools_executed": result.tool_calls,
                            }
                        )
                    )
                # One research cycle: a model call and the tools it asked for.
                cycle = clock() - last_call_started if last_call_started else 0.0
                if (
                    closes_on_limit
                    and not closed
                    and result.tool_calls
                    and remaining() - cycle < writing_reserve
                ):
                    # Research stops while the answer can still be written: one
                    # more cycle would eat into the writing reserve. The next
                    # call is offered no tool (see closing.py).
                    closed = True
                    messages.append(
                        {
                            "role": "user",
                            "content": time_closing_instruction(query.lang),
                        }
                    )
                    logging.getLogger(__name__).info(
                        json.dumps(
                            {
                                "event": "research_time_closing",
                                "remaining_seconds": round(remaining(), 1),
                                "tools_executed": result.tool_calls,
                            }
                        )
                    )
                prepare_context = getattr(type(model), "prepare_context", None)
                if prepare_context is not None:
                    await prepare_context(model, messages)
                reserved = model.estimate(messages)
                reserve(reserved)
                if isinstance(result, CodeResult):
                    result.model_calls += 1
                call_started = last_call_started = clock()
                received = await invoke(
                    model.complete(
                        messages,
                        # A call that writes a requested file gets 240 s: the gateway
                        # gives a first attempt half of it, and a whole researched
                        # document needs more than 60 s (cut twice on staging,
                        # 2026-11-10; the retry then had no budget left).
                        min(
                            240 if getattr(model, "produces_files", False) else 120,
                            remaining(),
                        )
                        if limits.code_mode
                        else remaining(),
                    )
                )
                if not isinstance(received, Turn):
                    raise ValueError("invalid_model_response")
                turn = received
                if not limits.code_mode:
                    logging.getLogger(__name__).info(
                        json.dumps(
                            {
                                "event": "chat_model_round",
                                "seconds": round(clock() - call_started, 1),
                                "calls": len(turn.calls),
                                "remaining_seconds": round(deadline - clock(), 1),
                            }
                        )
                    )
                if turn.usage is not None:
                    if (
                        turn.usage.tokens > reserved.tokens
                        or turn.usage.cost > reserved.cost
                    ):
                        raise ValueError("provider_usage_exceeds_reservation")
                    result.tokens -= reserved.tokens - turn.usage.tokens
                    result.cost -= reserved.cost - turn.usage.cost
            from services.orchestrator.narration import (
                protocol_material,
                unfinished_announcement,
            )

            if turn.calls and protocol_material(turn.text):
                turn = Turn("", turn.calls, turn.usage)
            if not turn.calls:
                if not turn.text.strip():
                    raise ValueError("empty_model_response")
                from services.orchestrator.narration import raw_tool_call

                complete_text = (
                    turn.text if final_only else "".join([*answer_parts, turn.text])
                )
                declared = getattr(model, "tools", None)
                offered = {
                    str(function["name"])
                    for tool in (declared if isinstance(declared, list) else [])
                    if isinstance(tool, dict)
                    and isinstance(function := tool.get("function"), dict)
                    and isinstance(function.get("name"), str)
                }
                raw = raw_tool_call(complete_text, offered)
                if raw is not None:
                    name, arguments = raw
                    turn = Turn(
                        "",
                        (Call(f"raw-tool-{result.tool_calls + 1}", name, arguments),),
                        turn.usage,
                    )
                elif protocol_material(complete_text) or unfinished_announcement(
                    complete_text
                ):
                    if protocol_recovered or not retry_protocol:
                        raise Stop("protocol_completion_limit")
                    protocol_recovered = True
                    messages.append(
                        {
                            "role": "user",
                            "content": "Return only the bounded final answer. Do not include internal request scope, tool history, or tool-call JSON.",
                        }
                    )
                    continue
                else:
                    from services.orchestrator.citation_cleanup import cleanup, hollow
                    from services.orchestrator.source_truth import (
                        without_unretrieved,
                        urls,
                    )
                    from services.orchestrator.link_requests import (
                        fallback_terms,
                        link_query,
                        unavailable,
                        instruction,
                        page_request,
                    )

                    cleaned = cleanup(
                        without_unretrieved(complete_text, retrieved | tool_urls)
                    )
                    if limits.code_mode and before_final is not None:
                        # A final answer the turn cannot accept yet (a requested
                        # run with nothing executed) goes back to the model once,
                        # with what it still has to do.
                        step = before_final([name for name, n in counts.items() if n])
                        if step is not None:
                            messages.append(
                                {"role": "assistant", "content": complete_text}
                            )
                            messages.append({"role": "user", "content": step})
                            answer_parts.clear()
                            continue
                    if limits.code_mode:
                        # Work preserves its prose when no external URL needs
                        # filtering; otherwise it removes only that URL.
                        result.text = (
                            cleaned
                            if urls(complete_text) - retrieved - tool_urls
                            else complete_text
                        )
                        result.state = "done"
                        setattr(result, "tool_urls", tool_urls)
                        return result
                    if page_request(
                        query.question,
                        tuple(str(m.get("content", "")) for m in (history or [])),
                    ) and (
                        hollow(complete_text, cleaned)
                        or not urls(cleaned).intersection(retrieved)
                    ):
                        terms = link_query(
                            query.question,
                            tuple(str(m.get("content", "")) for m in (history or [])),
                        )
                        if not links_recovered and not counts["web_search"]:
                            links_recovered = True
                            logging.getLogger(__name__).info(
                                "citation_guard_recovery search_attempt=1"
                            )
                            initial_calls = (
                                Call(
                                    "recover-links",
                                    "web_search",
                                    json.dumps({"query": terms, "lang": query.lang}),
                                ),
                            )
                            answer_parts.clear()
                            messages.append(
                                {"role": "system", "content": instruction()}
                            )
                            continue
                        complete_text = unavailable(
                            fallback_terms(
                                query.question,
                                tuple(
                                    str(m.get("content", "")) for m in (history or [])
                                ),
                            ),
                            query.lang,
                        )
                    result.text, result.state = complete_text, "done"
                    return result
            # Browser rendering retains interrupted prose. API clients receive
            # that narration as metadata, separate from the final answer.
            if include_intermediate:
                answer_parts.append(turn.text)
            elif turn.text:
                from services.orchestrator.stream_client import sink_context

                sink = sink_context.get()
                if sink:
                    await sink({"phase": "intermediate_narration", "text": turn.text})
            # In the chat a quota note is the last tool exchange: the model must now
            # answer. In Work it still has to write and publish the file it was
            # asked for (a researched note stopped here before its publication,
            # staging 2026-11-11): only more research is refused, below.
            if (quota_recovered or closed) and not limits.code_mode:
                raise Stop("tool_quota_exhausted")
            messages.append(
                {
                    "role": "assistant",
                    "content": turn.text,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": c.arguments},
                        }
                        for c in turn.calls
                    ],
                }
            )
            pending_calls = list(turn.calls)
            retried = False
            for call in pending_calls:
                if call not in turn.calls:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": call.id,
                                    "type": "function",
                                    "function": {
                                        "name": call.name,
                                        "arguments": call.arguments,
                                    },
                                }
                            ],
                        }
                    )
                result.state = "tool"
                remaining()
                try:
                    canonical = json.dumps(json.loads(call.arguments), sort_keys=True)
                except json.JSONDecodeError:
                    canonical = call.arguments
                consecutive = previous_call == (call.name, canonical)
                previous_call = call.name, canonical
                if (
                    call.name == "update_plan"
                    and last_plan is not None
                    and last_plan[0] == canonical
                ):
                    # Only reuse a successfully executed plan. Status/text changes
                    # still cross validation and persist as a new execution step.
                    acknowledgement: Message = dict(last_plan[1], unchanged=True)
                    if consecutive:
                        acknowledgement["next_step"] = (
                            "Act on the plan. Execute the next pending step or provide the requested answer."
                        )
                    if references is not None:
                        await references.retain(call, acknowledgement, messages)
                    else:
                        messages.append(
                            dict(
                                role="tool",
                                tool_call_id=call.id,
                                content=json.dumps(acknowledgement),
                            )
                        )
                    result.trace.append(
                        dict(
                            tool=call.name,
                            arguments=call.arguments,
                            output=acknowledgement,
                            reused=True,
                        )
                    )
                    continue
                if limit_refusal is not None and call.name in SEARCH_TOOLS:
                    # The search limit was reached earlier in this turn: the same
                    # refusal again, without another request or reservation.
                    if references is not None:
                        await references.retain(call, dict(limit_refusal), messages)
                    else:
                        messages.append(
                            dict(
                                role="tool",
                                tool_call_id=call.id,
                                content=json.dumps(limit_refusal, ensure_ascii=False),
                            )
                        )
                    result.trace.append(
                        dict(
                            tool=call.name,
                            arguments=call.arguments,
                            output=dict(limit_refusal),
                            reused=True,
                        )
                    )
                    continue
                quota = (
                    "tool_calls"
                    if result.tool_calls >= limits.tool_calls
                    else "search_limit"
                    if call.name in {"web_search", "web_images"}
                    and counts["web_search"] >= 3
                    else "fetch_limit"
                    if call.name == "web_fetch" and counts[call.name] >= 8
                    else ""
                )
                if (
                    quota
                    and limits.profile
                    # In Work the step ceiling stays a hard stop; a research quota is
                    # told to the model, as in the chat, so that it writes with what
                    # it has (a researched note died here, staging 2026-11-10). Work
                    # may be told a few times; the chat once.
                    and (
                        (
                            limits.code_mode
                            and quota != "tool_calls"
                            and quota_denials < 4
                        )
                        or (not limits.code_mode and not quota_recovered)
                    )
                ):
                    quota_recovered = True
                    quota_denials += 1
                    for denied in pending_calls[pending_calls.index(call) :]:
                        denial: Message = {
                            "error": "tool_quota_exhausted",
                            "reason": quota,
                            "tools_executed": result.tool_calls,
                            "tool_limit": limits.tool_calls,
                            "searches_executed": counts["web_search"],
                            "search_limit": 3,
                            "fetches_executed": counts["web_fetch"],
                            "fetch_limit": 8,
                        }
                        if references is not None:
                            await references.retain(denied, denial, messages)
                        else:
                            messages.append(
                                dict(
                                    role="tool",
                                    tool_call_id=denied.id,
                                    content=json.dumps(denial),
                                )
                            )
                    logging.getLogger(__name__).info(
                        json.dumps(
                            {
                                "event": "tool_quota_exhausted",
                                "reason": quota,
                                "executed": result.tool_calls,
                                "searches": counts["web_search"],
                                "search_limit": 3,
                                "fetches": counts["web_fetch"],
                                "fetch_limit": 8,
                                "tool_limit": limits.tool_calls,
                            }
                        )
                    )
                    break
                if result.tool_calls >= limits.tool_calls:
                    record_limit(
                        {
                            "kind": "tool_calls",
                            "measured": result.tool_calls,
                            "limit": limits.tool_calls,
                            "unit": "steps",
                        }
                    )
                    raise Stop("tool_calls")
                if (
                    call.name in {"web_search", "web_images"}
                    and counts["web_search"] >= 3
                ):
                    raise Stop("search_limit")
                if call.name == "web_fetch" and counts[call.name] >= 8:
                    raise Stop("fetch_limit")
                recent.append((call.name, canonical))
                if len(recent) >= 3 and recent[-1] == recent[-2] == recent[-3]:
                    if (call.name, canonical) in nudged:
                        raise Stop("loop_detected")
                    # Third identical call in a row: answer it once without running
                    # it, so the model can change course; a fourth one stops.
                    nudged.add((call.name, canonical))
                    nudge: Message = {
                        "error": "repeated_call",
                        "message": (
                            "This exact call was already made twice in a row and its "
                            "result will not change. Use the previous result, or change "
                            "the arguments (for edit_file, read the file again and copy "
                            "old_text exactly). Repeating it again stops the turn."
                        ),
                    }
                    last = result.trace[-1] if result.trace else {}
                    if last.get("tool") == call.name:
                        previous = json.dumps(last.get("output"), ensure_ascii=False)
                        if len(previous) <= 4000:
                            nudge["previous_result"] = last.get("output")
                    if references is not None:
                        await references.retain(call, nudge, messages)
                    else:
                        messages.append(
                            dict(
                                role="tool",
                                tool_call_id=call.id,
                                content=json.dumps(nudge),
                            )
                        )
                    result.trace.append(
                        dict(
                            tool=call.name,
                            arguments=call.arguments,
                            output=nudge,
                            reused=True,
                        )
                    )
                    logging.getLogger(__name__).info(
                        json.dumps({"event": "repeated_call_nudge", "tool": call.name})
                    )
                    continue
                reserve(tools.estimate(call))
                result.tool_calls += 1
                counts["web_search" if call.name == "web_images" else call.name] += 1
                tool_started = clock()
                output = await invoke(
                    references.reopen(call)
                    if references is not None and call.name == "reopen_result"
                    else tools.execute(
                        call, min(125 if limits.code_mode else 15, remaining())
                    )
                )
                if not isinstance(output, dict):
                    raise ValueError("invalid_tool_response")
                if limits.code_mode:
                    # Work steps are not in the interaction record: a looping turn
                    # (staging 2026-11-13, 21 calls to the wall clock) could not
                    # be read. Tool name, outcome code and duration only.
                    code = output.get("error")
                    logging.getLogger(__name__).info(
                        json.dumps(
                            {
                                "event": "work_tool",
                                "tool": call.name,
                                # A code, never a message that could quote content.
                                "error": code
                                if isinstance(code, str)
                                and re.fullmatch(r"[a-z_]{1,40}", code)
                                else (None if code is None else "other"),
                                "seconds": round(clock() - tool_started, 1),
                            }
                        )
                    )
                same_failures = 0
                if failures is not None:
                    output, same_failures = failures.record(call.name, output)
                if call.name == "update_plan" and "error" not in output:
                    last_plan = canonical, dict(output)
                from services.orchestrator.source_truth import tool_result_urls

                tool_urls.update(tool_result_urls(output))
                if (
                    retry_web
                    and not retried
                    and call.name == "web_search"
                    and output.get("error") in {"timeout", "search_unavailable"}
                ):
                    arguments = json.loads(call.arguments)
                    arguments["query"] = str(arguments["query"]) + " official source"
                    retry = Call(
                        call.id + "-retry",
                        call.name,
                        json.dumps(arguments, ensure_ascii=False),
                    )
                    pending_calls.append(retry)
                    retried = True
                if call.name in {"web_search", "web_fetch"} and "error" not in output:
                    data = output.get("data", output)
                    if isinstance(data, dict):
                        if isinstance(data.get("url"), str):
                            retrieved.add(str(data["url"]))
                        rows = data.get("results")
                        if isinstance(rows, list):
                            retrieved.update(
                                str(row["link"])
                                for row in rows
                                if isinstance(row, dict)
                                and isinstance(row.get("link"), str)
                            )
                if (
                    call.name == "web_search"
                    and not counts["web_fetch"]
                    and "error" not in output
                ):
                    from services.orchestrator.search_policy import requested_page_read

                    data = output.get("data", output)
                    rows = data.get("results") if isinstance(data, dict) else None
                    if requested_page_read(
                        query.question,
                        tuple(str(m.get("content", "")) for m in (history or [])),
                    ) and isinstance(rows, list):
                        from services.orchestrator.link_requests import page_request

                        reads: list[Call] = list(initial_calls)
                        for row in rows:
                            if len(reads) >= 3:
                                break
                            if isinstance(row, dict) and isinstance(
                                row.get("link"), str
                            ):
                                reads.append(
                                    Call(
                                        call.id
                                        + "-read"
                                        + (str(len(reads)) if reads else ""),
                                        "web_fetch",
                                        json.dumps(
                                            {
                                                "url": row["link"],
                                                "query": query.question,
                                            }
                                        ),
                                    )
                                )
                                if len(reads) >= (
                                    3
                                    if page_request(
                                        query.question,
                                        tuple(
                                            str(m.get("content", ""))
                                            for m in (history or [])
                                        ),
                                    )
                                    else 1
                                ):
                                    break
                        initial_calls = tuple(reads)
                event: Message = {
                    "tool": call.name,
                    "arguments": call.arguments,
                    "output": output,
                }
                result.trace.append(event)
                logging.getLogger(__name__).info(
                    json.dumps(
                        {
                            "event": "tool_finished",
                            "tool": call.name,
                            "count": result.tool_calls,
                        }
                    )
                )
                if references is not None:
                    await references.retain(call, output, messages)
                else:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(output, ensure_ascii=False),
                        }
                    )
                if closes_on_limit and limit_refusal is None and limit_code(output):
                    limit_refusal = dict(output)
                if same_failures:
                    logging.getLogger(__name__).info(
                        json.dumps(
                            {
                                "event": "repeated_tool_failure",
                                "tool": call.name,
                                "count": same_failures,
                            }
                        )
                    )
                    record_limit(
                        {
                            "kind": STOP_REASON,
                            "measured": same_failures,
                            "limit": SAME_FAILURE_STOP,
                            "unit": "calls",
                        }
                    )
                    raise Stop(STOP_REASON)
                if call.name in terminal_tools and "error" not in output:
                    result.text = (
                        next(
                            (part for part in reversed(answer_parts) if part.strip()),
                            "",
                        )
                        if final_only
                        else "".join(answer_parts)
                    )
                    result.state = "done"
                    return result
                if output.get("state") == "awaiting_approval":
                    try:
                        arguments = json.loads(call.arguments)
                        command = str(arguments.get("command", "the requested command"))
                        target = str(
                            output.get(
                                "target_name", arguments.get("target", "the target")
                            )
                        )
                    except (TypeError, ValueError):
                        command, target = "the requested command", "the target"
                    waiting = {
                        "fr": "en attente de votre approbation pour exécuter {command} sur {target}",
                        "en": "waiting for your approval to run {command} on {target}",
                        "de": "wartet auf Ihre Freigabe, um {command} auf {target} auszuführen",
                        "es": "esperando tu aprobación para ejecutar {command} en {target}",
                        "it": "in attesa della tua approvazione per eseguire {command} su {target}",
                    }
                    result.text = waiting[query.lang].format(
                        command=command, target=target
                    )
                    result.state = "done"
                    return result
    except Stop as exc:
        result.reason = str(exc)
    except ValidationError as exc:
        result.state, result.reason, result.text = (
            "stopped",
            "provider_error",
            describe_validation(exc),
        )
        return result
    except LimitError as exc:
        result.state, result.reason, result.text = "stopped", exc.code, exc.detail
        return result
    except GatewayError as exc:
        if exc.code != "cost_budget" or not limits.code_mode:
            raise
        result.reason = "cost"
    except PublicFailure:
        raise
    except (ValueError, RuntimeError, OSError):
        result.reason = "provider_error"
    result.state = "stopped"
    result.text = (
        f"Explicit stop: {result.reason}. Tools executed: {result.tool_calls}; limit {limits.tool_calls}. "
        f"Tokens reserved: {result.tokens}; limit {limits.tokens}. "
        f"Cost reserved: {result.cost} EUR; limit {limits.cost} EUR. "
        f"Elapsed: {max(0, clock() - (deadline - limits.wall_clock)):.3f} seconds; "
        f"limit {limits.wall_clock:.3f} seconds. "
        f"Searches: {counts['web_search']}; limit 3. Fetches: {counts['web_fetch']}; limit 8."
    )
    if links_recovered:
        from services.orchestrator.link_requests import fallback_terms, unavailable

        result.text = (
            unavailable(
                fallback_terms(
                    query.question,
                    tuple(str(m.get("content", "")) for m in (history or [])),
                ),
                query.lang,
            )
            + "\n\n"
            + result.text
        )
    if isinstance(result, CodeResult) and result.limit_stop:
        result.text += (
            " Required/measured "
            f"{display_limit_value(result.limit_stop['measured'])} "
            f"{result.limit_stop['unit']}; limit "
            f"{display_limit_value(result.limit_stop['limit'])} "
            f"{result.limit_stop['unit']}."
        )
    logging.getLogger(__name__).info(
        json.dumps(
            {
                "event": "request_stopped",
                "reason": result.reason,
                "tokens_reserved": result.tokens,
                "cost_reserved": str(result.cost),
            }
        )
    )
    return result


def display_limit_value(value: object) -> str:
    """Format limit diagnostics for people, without scientific notation."""
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)
