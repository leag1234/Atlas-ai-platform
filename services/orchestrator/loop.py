"""POC-P6: reserve worst-case usage before starting cancellable I/O."""

from packages.profiles import PROFILES
from packages.limits import LimitError

import asyncio
import json
import logging
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
            from services.orchestrator.code_workspace import enabled

            if not enabled() or not (
                self.tokens is None
                and self.profile in PROFILES
                and 0 <= self.tool_calls <= 1000
                and 0 <= self.wall_clock <= 86400
                and self.cost.is_finite()
                and 0 <= self.cost <= 100
                and self.model_calls is not None
                and 0 <= self.model_calls <= 1001
            ):
                raise ValueError("invalid_code_limits")
            return
        if self.profile is not None and self.profile not in PROFILES:
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
    quota_recovered = False
    protocol_recovered = False
    links_recovered = False
    retrieved: set[str] = set()
    answer_parts: list[str] = []

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
                reserved = model.estimate(messages)
                reserve(reserved)
                if isinstance(result, CodeResult):
                    result.model_calls += 1
                received = await invoke(
                    model.complete(
                        messages,
                        min(120, remaining()) if limits.code_mode else remaining(),
                    )
                )
                if not isinstance(received, Turn):
                    raise ValueError("invalid_model_response")
                turn = received
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

                complete_text = "".join([*answer_parts, turn.text])
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
                        link_query,
                        unavailable,
                        instruction,
                        page_request,
                    )

                    cleaned = cleanup(without_unretrieved(complete_text, retrieved))
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
                        complete_text = unavailable(terms, query.lang)
                    result.text, result.state = complete_text, "done"
                    return result
            # Streaming already exposes this content; retain it in the final
            # HTTP answer and journal as well when a tool interrupts the prose.
            answer_parts.append(turn.text)
            if quota_recovered:
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
                    and not quota_recovered
                    and not limits.code_mode
                ):
                    quota_recovered = True
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
                try:
                    canonical = json.dumps(json.loads(call.arguments), sort_keys=True)
                except json.JSONDecodeError:
                    canonical = call.arguments
                recent.append((call.name, canonical))
                if len(recent) >= 3 and recent[-1] == recent[-2] == recent[-3]:
                    raise Stop("loop_detected")
                reserve(tools.estimate(call))
                result.tool_calls += 1
                counts["web_search" if call.name == "web_images" else call.name] += 1
                output = await invoke(
                    references.reopen(call)
                    if references is not None and call.name == "reopen_result"
                    else tools.execute(
                        call, min(125 if limits.code_mode else 15, remaining())
                    )
                )
                if not isinstance(output, dict):
                    raise ValueError("invalid_tool_response")
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
                if call.name in terminal_tools and "error" not in output:
                    result.text = "".join(answer_parts)
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
        from services.orchestrator.link_requests import unavailable, link_query

        result.text = (
            unavailable(
                link_query(
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
