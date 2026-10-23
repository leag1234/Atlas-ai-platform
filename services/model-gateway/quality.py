"""M21 profiles share one ledger and at most one affordable recovery attempt."""

from collections.abc import AsyncGenerator
from contextlib import aclosing
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import json
import asyncio
import copy
import logging
from time import monotonic
from pathlib import Path

from agent_provider import AgentProvider, AgentRequest, Usage, classify
from answer_content import (
    ANSWER_NOW,
    is_promise_only,
    failed_terminal_content,
    possible_promise_answer,
    missing_file_failure,
    strip_file_promises,
)
from packages.context_limit import ContextExceeded
from provider_retry import TransientProviderError
from packages.limits import (
    ProviderFailure,
    LimitError,
    ProviderLimitError,
    MalformedToolCall,
)

from packages.tool_history import (
    final_messages,
    is_history_answer,
    possible_history_answer,
)
from packages.profiles import PROFILES
from packages.evidence import estimated_tokens
from packages.images import image_info
from serverless import ServerlessPolicy, classify_task


CONTEXT_TRUNCATION_MARKER = "\n[atlas: fetched page truncated to fit model context]\n"


def trim_fetched_pages(
    messages: list[dict[str, object]],
    tools: list[dict[str, object]],
    maximum: int,
) -> list[dict[str, object]]:
    """Retain a marked prefix of the largest fetched pages within the model limit."""
    trimmed = copy.deepcopy(messages)
    fetches: set[str] = set()
    for message in trimmed:
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if (
                isinstance(function, dict)
                and function.get("name") == "web_fetch"
                and isinstance(call.get("id"), str)
            ):
                fetches.add(call["id"])
    pages: list[tuple[int, str]] = []
    for index, message in enumerate(trimmed):
        if message.get("role") != "tool" or message.get("tool_call_id") not in fetches:
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            output = json.loads(content)
        except json.JSONDecodeError:
            continue
        data = output.get("data") if isinstance(output, dict) else None
        if isinstance(data, dict) and isinstance(data.get("text"), str):
            pages.append((index, data["text"]))
    for index, text in sorted(pages, key=lambda page: len(page[1]), reverse=True):
        while input_bound(trimmed, tools) > maximum:
            content = trimmed[index]["content"]
            assert isinstance(content, str)
            output = json.loads(content)
            assert isinstance(output, dict)
            data = output["data"]
            assert isinstance(data, dict)
            current = str(data["text"])
            retained_text = current.removesuffix(CONTEXT_TRUNCATION_MARKER)
            if not retained_text:
                break
            excess = input_bound(trimmed, tools) - maximum
            retained = max(0, len(retained_text) - excess)
            data["text"] = retained_text[:retained] + CONTEXT_TRUNCATION_MARKER
            data["truncated"] = True
            data["context_truncated"] = True
            trimmed[index]["content"] = json.dumps(output, ensure_ascii=False)
        if input_bound(trimmed, tools) <= maximum:
            break
    return trimmed


def input_bound(
    messages: list[dict[str, object]], tools: list[dict[str, object]]
) -> int:
    normalized = copy.deepcopy(messages)
    pixel_tokens = count = byte_count = pixels = 0
    for message in normalized:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            image = part.get("image_url")
            if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                raise ValueError("invalid_image")
            info = image_info(image["url"])
            width, height = int(info["width"]), int(info["height"])
            pixel_tokens += ((width + 15) // 16 + 1) * ((height + 15) // 16) + 1
            count += 1
            byte_count += int(info["bytes"])
            pixels += width * height
            image["url"] = "[image]"
    for measured, maximum, unit in (
        (count, 4, "images"),
        (byte_count, 4 * 1024 * 1024, "bytes"),
        (pixels, 16000000, "pixels"),
    ):
        if measured > maximum:
            raise LimitError("image_size_exceeded", measured, maximum, unit)
    # Reserve the larger representation before I/O, including answer-only JSON
    # escaping. Tool schemas are retained in this bound even if not transmitted.
    return (
        max(
            len(
                json.dumps(
                    {"messages": value, "tools": tools}, ensure_ascii=False
                ).encode()
            )
            for value in (normalized, final_messages(normalized))
        )
        + 256
        + pixel_tokens
        + (1024 if count else 0)
    )


async def complete(provider: AgentProvider, request: AgentRequest) -> dict[str, object]:
    async with aclosing(stream_quality(provider, request)) as events:
        async for event in events:
            result = event.get("result")
            if isinstance(result, dict):
                return result
    raise RuntimeError("provider_response_invalid")


async def stream_quality(
    provider: AgentProvider, request: AgentRequest
) -> AsyncGenerator[dict[str, object], None]:
    from stream_transport import attempt, response_window

    policy = ServerlessPolicy()
    task = classify_task(request.messages)
    role = (
        "vision"
        if task == "vision"
        else policy.public_profiles.get(request.profile or "", task)
    )
    primary = policy.models[role]
    fallback_role = policy.config[role]["fallback"]
    budget = Decimal(request.budget_eur or "0.10")
    incoming = input_bound(request.messages, request.tools)
    spent = Decimal(0)
    prompt_tokens = completion_tokens = trace_tokens = answer_tokens = 0
    retry_completion_tokens = 0
    reasoning = ""
    estimated = retried = fallback = False
    started = monotonic()
    current = request
    model = primary
    malformed_name: MalformedToolCall | None = None

    def select_affordable_model() -> None:
        nonlocal role, model, fallback_role, fallback
        alternate = policy.models[fallback_role]
        if policy.cost(model, incoming, 1) > budget:
            selected = None
            if policy.cost(alternate, incoming, 1) <= budget:
                selected = fallback_role
            elif task != "vision":
                # Preserve complete attachment context when both configured expert
                # roles exceed the cap. Only already configured public text roles are
                # eligible; no input sampling or larger cost allowance is permitted.
                affordable_roles = [
                    candidate
                    for candidate in dict.fromkeys(policy.public_profiles.values())
                    if incoming + 1
                    <= policy.config[candidate]["capabilities"][
                        policy.models[candidate]
                    ]["context"]
                    and policy.cost(policy.models[candidate], incoming, 1) <= budget
                ]
                if affordable_roles:
                    selected = min(
                        affordable_roles,
                        key=lambda candidate: policy.cost(
                            policy.models[candidate], incoming, request.max_tokens
                        ),
                    )
                    fallback_role = policy.config[selected]["fallback"]
            if selected is not None:
                role, model = selected, policy.models[selected]
                fallback = True
                logging.getLogger(__name__).info(
                    json.dumps(
                        {
                            "event": "quality_affordable_alternate",
                            "budget_eur": str(budget),
                        }
                    )
                )

    select_affordable_model()
    # Research is optional once another selection turn plus the full-context
    # answer no longer fit. Keep all acquired evidence and the selected effort.
    # File operations are the requested deliverable, so an answer-only turn
    # cannot substitute for them. Each attempt still reserves its full bound
    # below; lacking funds must stop explicitly rather than promise a file.
    file_tools = any(
        isinstance(function := tool.get("function"), dict)
        and function.get("name") in {"terminal_command", "publish_document"}
        for tool in request.tools
    )
    terminal_failed = any(
        m.get("role") == "tool"
        and '"terminal_operation_failed"' in str(m.get("content", ""))
        for m in request.messages
    )
    evidence_without_tools = not request.tools and any(
        message.get("role") == "tool" for message in request.messages
    )
    if (
        terminal_failed
        or evidence_without_tools
        or (
            request.tools
            and not file_tools
            and (
                policy.cost(model, incoming, request.max_tokens)
                + policy.cost(model, incoming, 3000)
                > budget
            )
        )
    ):
        request = request.model_copy(
            update={
                "tool_choice": "none",
                # Research finalization presupposes acquired evidence. Before
                # any tool result, preserve the original task and disposition.
                "messages": [
                    *(
                        final_messages(request.messages)
                        if terminal_failed
                        else request.messages
                    ),
                    *(
                        [
                            {
                                "role": "system",
                                "content": Path(
                                    "prompts/file-failure.txt"
                                    if any(
                                        m.get("role") == "tool"
                                        and '"terminal_operation_failed"'
                                        in str(m.get("content", ""))
                                        for m in request.messages
                                    )
                                    else "prompts/budget-answer.txt"
                                ).read_text()
                                + ("\n" + ANSWER_NOW if terminal_failed else ""),
                            }
                        ]
                        if any(m.get("role") == "tool" for m in request.messages)
                        else []
                    ),
                ],
            }
        )
        current = request
        incoming = input_bound(request.messages, request.tools)
        logging.getLogger(__name__).info(
            json.dumps({"event": "quality_answer_budget", "remaining_eur": str(budget)})
        )
    # Finalization instructions may invalidate the initial affordability check.
    select_affordable_model()
    for index in range(2):
        remaining = request.timeout - (monotonic() - started)
        if remaining <= 0:
            if malformed_name is not None:
                raise MalformedToolCall(malformed_name.measured, index) from None
            raise TimeoutError("timeout")
        # Retain room for one bounded recovery, not all future tool turns.
        # Actual cost is reserved immediately before each transport call.
        recovery = (
            policy.cost(policy.models[fallback_role], incoming, 1000)
            if index == 0
            else Decimal(0)
        )
        if (
            index == 0
            and request.profile == PROFILES[0]
            and recovery + policy.cost(model, incoming, current.max_tokens)
            > budget - spent
        ):
            recovery = Decimal(0)
        if recovery + policy.cost(model, incoming, 1) > budget - spent:
            recovery = Decimal(0)
        if (
            index
            or policy.cost(model, incoming, current.max_tokens)
            > budget - spent - recovery
        ):
            available = budget - spent - recovery - policy.cost(model, incoming, 0)
            price = policy.cost(model, 0, 1)
            affordable = (
                int((available / price).to_integral_value(rounding=ROUND_FLOOR))
                if price
                else 3000
            )
            if affordable < 1:
                raise ProviderLimitError(
                    "cost_budget",
                    int(
                        (policy.cost(model, incoming, 1) * 1_000_000).to_integral_value(
                            rounding=ROUND_CEILING
                        )
                    ),
                    int(
                        ((budget - spent) * 1_000_000).to_integral_value(
                            rounding=ROUND_FLOOR
                        )
                    ),
                    "microEUR",
                )
            current = request.model_copy(
                update={
                    "max_tokens": min(
                        3000 if index else request.max_tokens, affordable
                    ),
                    "reasoning_effort": "none" if index else request.reasoning_effort,
                }
            )
        reserved = policy.cost(model, incoming, current.max_tokens)
        if spent + reserved > budget:
            raise ProviderLimitError(
                "cost_budget",
                int(
                    ((spent + reserved) * 1_000_000).to_integral_value(
                        rounding=ROUND_CEILING
                    )
                ),
                int((budget * 1_000_000).to_integral_value(rounding=ROUND_FLOOR)),
                "microEUR",
            )
        capabilities = policy.config[role]["capabilities"][model]
        if incoming + current.max_tokens > capabilities["context"]:
            messages = trim_fetched_pages(
                request.messages,
                request.tools,
                capabilities["context"] - current.max_tokens,
            )
            if messages != request.messages:
                request = request.model_copy(update={"messages": messages})
                current = current.model_copy(update={"messages": messages})
                incoming = input_bound(request.messages, request.tools)
                logging.getLogger(__name__).info(
                    json.dumps(
                        {
                            "event": "research_context_trimmed",
                            "incoming": incoming,
                            "context": capabilities["context"],
                        }
                    )
                )
            if incoming + current.max_tokens > capabilities["context"]:
                raise ContextExceeded(
                    incoming + current.max_tokens, capabilities["context"]
                )
        # Track the pre-validation streaming decision separately from withholding
        # tool-enabled text. Only malformed names may undo that decision.
        answer_started = False
        answer_emitted = False
        held_content: list[dict[str, object]] = []
        buffered: list[dict[str, object]] = []
        prefix = ""
        prefix_open = True
        partial_reasoning = ""
        result: dict[str, object] | None = None
        # Reserve time for recovery, still under the original request deadline.
        allotted = remaining * 0.8 if index == 0 else remaining
        full_deadline = asyncio.get_running_loop().time() + remaining
        try:
            async with (
                response_window(remaining),
                asyncio.timeout(allotted) as deadline,
                aclosing(attempt(current, model, allotted)) as events,
            ):
                async for event in events:
                    terminal = event.get("result")
                    if isinstance(terminal, dict):
                        result = terminal
                        break
                    delta = event.get("delta")
                    if isinstance(delta, dict):
                        if isinstance(delta.get("reasoning_content"), str):
                            partial_reasoning += str(delta["reasoning_content"])
                    if isinstance(delta, dict) and delta.get("content"):
                        if current.max_tokens < request.max_tokens or terminal_failed:
                            buffered.append(event)
                            continue
                        if prefix_open:
                            prefix += str(delta["content"])
                            buffered.append(event)
                            if possible_history_answer(
                                prefix
                            ) or possible_promise_answer(prefix):
                                continue
                            prefix_open = False
                            # Visible answers cannot be retried. Let this attempt
                            # use the original deadline instead of cancelling it
                            # early to reserve time for an ineligible recovery.
                            deadline.reschedule(full_deadline)
                            for pending_event in buffered:
                                if current.tools and not answer_emitted:
                                    held_content.append(pending_event)
                                else:
                                    yield pending_event
                                    answer_emitted = True
                            buffered.clear()
                        elif current.tools and not answer_emitted:
                            held_content.append(event)
                        else:
                            yield event
                            answer_emitted = True
                        answer_started = True
                        # Keep a short preamble recoverable if a malformed tool
                        # name follows it, without withholding the entire answer.
                        # Once visible, a late provider error must stop, not retry.
                        if (
                            sum(
                                len(str(held_delta.get("content", "")))
                                for part in held_content
                                if isinstance(held_delta := part.get("delta"), dict)
                            )
                            > 128
                        ):
                            for pending_event in held_content:
                                yield pending_event
                            held_content.clear()
                            answer_emitted = True
                        continue
                    yield event
            if result is None:
                raise RuntimeError("provider_response_invalid")
        except ProviderLimitError:
            for event in held_content:
                yield event
            raise
        except (RuntimeError, TimeoutError, LimitError) as exc:
            if isinstance(exc, MalformedToolCall):
                malformed_name = exc
                if current.tools and not answer_emitted:
                    answer_started = False
            else:
                for event in held_content:
                    yield event
            # Invalid provider fields are recoverable output failures. Keep the
            # same decoder limits and charge the unknown attempt in full.
            # Request/output budgets must never enter this recovery path.
            if isinstance(exc, LimitError) and exc.code not in {
                "provider_response_limit",
                "tool_limit",
            }:
                raise
            failed_usage = (
                exc.usage if isinstance(exc, TransientProviderError) else None
            )
            if failed_usage is not None:
                if (
                    failed_usage.prompt_tokens > incoming
                    or failed_usage.completion_tokens > current.max_tokens
                ):
                    raise RuntimeError("provider_usage_exceeds_reservation") from None
                spent += policy.cost(
                    model, failed_usage.prompt_tokens, failed_usage.completion_tokens
                )
                prompt_tokens += failed_usage.prompt_tokens
                completion_tokens += failed_usage.completion_tokens
                retry_completion_tokens += failed_usage.completion_tokens
            else:
                spent += reserved
                prompt_tokens += incoming
                completion_tokens += current.max_tokens
                retry_completion_tokens += current.max_tokens
                estimated = True
            # Keep the trace actually shown before an interrupted attempt.
            # Its token count is estimated; unknown billing stays fully reserved.
            reasoning += partial_reasoning
            trace_tokens += estimated_tokens(partial_reasoning)
            logging.getLogger(__name__).warning(
                json.dumps(
                    {
                        "event": "quality_attempt_failed",
                        "category": "malformed_tool_call"
                        if isinstance(exc, MalformedToolCall)
                        else "timeout"
                        if isinstance(exc, TimeoutError)
                        else "provider",
                        "attempt": index + 1,
                        "elapsed_seconds": round(monotonic() - started, 3),
                        "reserved_unknown_eur": str(spent),
                        "final_usage_measured": failed_usage is not None,
                        "visible_content": answer_started,
                    }
                )
            )
            if index or answer_started:
                if malformed_name is not None and not isinstance(exc, LimitError):
                    raise MalformedToolCall(malformed_name.measured, index) from None
                if isinstance(exc, (TimeoutError, LimitError)):
                    raise
                raise ProviderFailure(
                    str(exc), getattr(exc, "finish_reason", None), index
                ) from None
            role = fallback_role
            model = policy.models[role]
            fallback = True
            retried = False
            if isinstance(exc, MalformedToolCall):
                # Keep the original history and its reservation; the rejected
                # name is never sent back. No additional retry is introduced.
                request = request.model_copy(update={"tool_choice": "none"})
            yield {"phase": "reasoning_fallback" if retried else "provider_fallback"}
            continue
        for event in held_content:
            yield event
        usage = Usage.model_validate(result["usage"])
        if (
            usage.prompt_tokens > incoming
            or usage.completion_tokens > current.max_tokens
        ):
            raise RuntimeError("provider_usage_exceeds_reservation")
        spent += policy.cost(model, usage.prompt_tokens, usage.completion_tokens)
        prompt_tokens += usage.prompt_tokens
        completion_tokens += usage.completion_tokens
        trace = str(result.get("reasoning", ""))
        reasoning += trace
        count = result.get("reasoning_tokens")
        if type(count) is not int:
            estimated |= bool(trace)
            count = (
                max(
                    0,
                    usage.completion_tokens
                    - estimated_tokens(str(result.get("text", ""))),
                )
                if trace
                else 0
            )
        trace_tokens += count
        answer_tokens += usage.completion_tokens - count
        malformed_arguments = False
        calls = result.get("calls")
        if isinstance(calls, list):
            for call in calls:
                if isinstance(call, dict):
                    try:
                        arguments = json.loads(str(call.get("arguments", "")))
                        malformed_arguments |= not isinstance(arguments, dict)
                    except ValueError:
                        malformed_arguments = True
        if malformed_arguments:
            logging.getLogger(__name__).warning(
                json.dumps({"event": "invalid_tool_arguments", "attempt": index + 1})
            )
            if index or answer_started:
                if malformed_name is not None:
                    raise MalformedToolCall(malformed_name.measured, index)
                raise RuntimeError("invalid_tool_arguments")
        malformed = is_history_answer(str(result.get("text", "")))
        if malformed:
            logging.getLogger(__name__).warning(
                json.dumps(
                    {
                        "event": "unexpected_tool_history_answer",
                        "content_chars": len(str(result.get("text", ""))),
                        "completion_tokens": usage.completion_tokens,
                        "attempt": index + 1,
                    }
                )
            )
            if index or answer_started:
                if malformed_name is not None:
                    raise MalformedToolCall(malformed_name.measured, index)
                raise RuntimeError("unexpected_tool_history_answer")
        future_action = (
            terminal_failed
            and not calls
            and (
                strip_file_promises(str(result.get("text", "")))
                != result.get("text", "")
            )
        )
        if future_action and index:
            # The single retry is exhausted. Only validated substantive content
            # and the failure disclosure may survive; no rejected delta escapes.
            result["text"] = strip_file_promises(str(result["text"]))
            buffered = [{"delta": {"content": result["text"]}}]
            future_action = False
        invalid_content = terminal_failed and failed_terminal_content(
            str(result.get("text", "")), request.messages
        )
        promise_only = not calls and (
            is_promise_only(str(result.get("text", "")))
            or invalid_content
            or future_action
        )
        missing_failure = (
            terminal_failed
            and not calls
            and missing_file_failure(str(result.get("text", "")))
        )
        delivery_error = (
            "promise_only_final_answer" if promise_only else "missing_file_failure"
        )
        if promise_only or missing_failure:
            logging.getLogger(__name__).warning(
                json.dumps({"event": delivery_error, "attempt": index + 1})
            )
            if index or answer_started:
                raise ProviderFailure(
                    delivery_error, str(result.get("finish_reason")), index
                )
            request = request.model_copy(
                update={
                    "tool_choice": "none",
                    "messages": [
                        *request.messages,
                        {
                            "role": "system",
                            "content": (
                                str(request.messages[-1]["content"])
                                if terminal_failed
                                else ANSWER_NOW
                            )
                            + (
                                " The last response was rejected: deliver the content itself, "
                                "not document-generation code, intentions, or an apology. "
                                "Keep only a brief failure disclosure before the requested content. "
                                "Remove EVERY sentence promising a future file action or retry, "
                                "even when the rest of the answer already contains useful content."
                                if terminal_failed
                                else ""
                            ),
                        },
                    ],
                }
            )
            incoming = input_bound(request.messages, request.tools)
        incomplete = result.get("finish_reason") == "length"
        if incomplete:
            logging.getLogger(__name__).warning(
                json.dumps(
                    {
                        "event": "provider_output_incomplete",
                        "finish_reason": "length",
                        "completion_tokens": usage.completion_tokens,
                        "max_tokens": current.max_tokens,
                        "content_chars": len(str(result.get("text", ""))),
                        "attempt": index + 1,
                    }
                )
            )
            if index or answer_started:
                if malformed_name is not None:
                    raise MalformedToolCall(malformed_name.measured, index)
                if index and (not str(result.get("text", "")).strip()):
                    raise ProviderFailure(
                        "empty_content_after_retry",
                        str(result.get("finish_reason")),
                        index,
                    )
                raise ProviderFailure(
                    "incomplete_provider_answer",
                    str(result.get("finish_reason")),
                    index,
                )
        # Empty terminal responses retain validated billing, then get one retry.
        if (
            promise_only
            or missing_failure
            or malformed
            or malformed_arguments
            or incomplete
            or (not str(result.get("text", "")).strip() and not result.get("calls"))
        ):
            if index:
                if malformed_name is not None:
                    raise MalformedToolCall(malformed_name.measured, index)
                raise ProviderFailure(
                    "empty_content_after_retry", str(result.get("finish_reason")), index
                )
            # A large evidence input may no longer fit the original provider's
            # conservative reservation. Try the configured alternate before
            # rejecting a recovery that still fits the same request ledger.
            alternate = policy.models[fallback_role]
            if trace.strip():
                logging.getLogger(__name__).warning(
                    json.dumps(
                        {
                            "event": "provider_default_regression",
                            "effort": current.reasoning_effort,
                            "trace_tokens": count,
                            "attempt": index + 1,
                        }
                    )
                )
            if result.get("finish_reason") != "length" and not trace.strip():
                role, model = fallback_role, alternate
                fallback = True
            if (
                policy.cost(model, incoming, 3000) > budget - spent
                and policy.cost(alternate, incoming, 3000) <= budget - spent
            ):
                role, model = fallback_role, alternate
                fallback = True
            retried = True
            yield {"phase": "reasoning_fallback"}
            continue
        for event in buffered:
            yield event
        answer = {
            "provider_model": model,
            "text": result["text"],
            "calls": result["calls"],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
            "cost_eur": str(spent),
            "reasoning": reasoning,
            "trace_tokens": trace_tokens,
            "answer_tokens": answer_tokens,
            "token_split_estimated": estimated,
            "reasoning_retried": retried,
            "provider_retries": index,
        }
        if retry_completion_tokens:
            answer["retry_completion_tokens"] = retry_completion_tokens
        if request.observe:
            answer["observation"] = {
                "provider": "escalade",
                "route": "simple"
                if classify(request.messages) == "chat_simple"
                else "complexe",
                "task_type": task,
                "fallback": fallback,
            }
        logging.getLogger(__name__).info(
            json.dumps(
                {
                    "event": "quality_usage",
                    "provider_model": model,
                    "profile": request.profile,
                    "effort": request.reasoning_effort,
                    "trace_tokens": trace_tokens,
                    "answer_tokens": answer_tokens,
                    "token_split_estimated": estimated,
                    "cost_eur": str(spent),
                    "reasoning_retried": retried,
                }
            )
        )
        yield {"result": answer}
        return
    raise RuntimeError("provider_error")
