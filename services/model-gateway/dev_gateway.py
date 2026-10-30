"""M15 code transport with a frozen, conservatively priced request."""

from packages.limits import LimitError

from collections.abc import AsyncGenerator
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_CEILING
import json
import os
import logging
import hashlib
from pathlib import Path
from typing import Any, Literal

import aiohttp
from pydantic import BaseModel, Field
from packages.dev_request import Request
from serverless import ServerlessPolicy
import provider_settings
from api_routing import Target, targets
from provider_retry import TransientProviderError
from streaming import Delta
from stream_usage import validate_usage


@dataclass(frozen=True)
class Plan:
    body: str
    endpoint: str
    reserved: int
    input_rate: Decimal
    output_rate: Decimal
    provider: str = ""
    model: str = ""
    api_key: str = ""
    prompt_cache_key: str | None = None
    cached_input_rate: Decimal | None = None
    unpriced: bool = False
    fallback: "Plan | None" = None
    attempt_bound: int = 0

    def charge(self, incoming: int, outgoing: int, cached: int = 0) -> int:
        cached = min(max(cached, 0), incoming)
        input_rate = self.input_rate
        if self.cached_input_rate is not None:
            input_rate = (
                incoming - cached
            ) * self.input_rate + cached * self.cached_input_rate
            return int(
                (input_rate + outgoing * self.output_rate).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
        return int(
            (
                incoming * self.input_rate + outgoing * self.output_rate
            ).to_integral_value(rounding=ROUND_CEILING)
        )


def _cache_key(api_key_id: str, body: dict[str, Any]) -> str:
    messages = body.get("messages")
    first = messages[0] if isinstance(messages, list) and messages else {}
    digest = hashlib.sha256(
        json.dumps(first, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return hashlib.sha256((api_key_id + ":" + digest).encode()).hexdigest()


def prepare_api(request: Any, usage: str, api_key_id: str) -> Plan:
    """Freeze a public usage route; raw IDs never enter this decision."""
    body = request.model_dump(exclude_none=True, by_alias=True)
    # Same wire cleanup as prepare(): strict providers reject empty tool_calls,
    # empty tool lists and an unrequested response_format with HTTP 422.
    if "response_format" not in request.model_fields_set:
        body.pop("response_format", None)
    for message in body["messages"]:
        if not message.get("tool_calls"):
            message.pop("tool_calls", None)
    if not request.tools or request.tool_choice == "none":
        for key in ("tools", "tool_choice", "parallel_tool_calls"):
            body.pop(key, None)
    effort = str(body.get("reasoning_effort", "none"))
    _, entries = targets(usage, effort)
    incoming = len(json.dumps(body, ensure_ascii=False).encode()) + 512
    # Estimate tokens as bytes / 4. The reservation remains conservative in
    # bytes, but is clamped to each target's advertised context.
    estimated_tokens = incoming // 4
    fitting = [entry for entry in entries if entry.context >= estimated_tokens]
    if not fitting:
        raise LimitError(
            "context_exceeded", estimated_tokens, entries[0].context, "tokens"
        )

    def one(target: Target) -> Plan:
        prices = ServerlessPolicy(allow_unpriced=True).prices
        price = prices[target.model]
        endpoint = str(
            provider_settings.preset_value(target.provider, "base_url")
        ).rstrip("/")
        payload = dict(
            body,
            model=target.model,
            stream=True,
            stream_options={"include_usage": True},
        )
        if target.effort is not None:
            # Measured 2026-10-30: some routed models accept only low, high and max.
            payload["reasoning_effort"] = target.effort
        if provider_settings.preset_value(target.provider, "prompt_cache_key"):
            payload["prompt_cache_key"] = _cache_key(api_key_id, payload)
        return Plan(
            json.dumps(payload, ensure_ascii=False),
            endpoint,
            0,
            Decimal(str(price["input_eur_per_mtok"])),
            Decimal(str(price["output_eur_per_mtok"])),
            provider=target.provider,
            model=target.model,
            api_key=provider_settings.api_key_for(target.provider),
            prompt_cache_key=payload.get("prompt_cache_key"),
            cached_input_rate=(
                Decimal(str(price["cached_input_eur_per_mtok"]))
                if price.get("cached_input_eur_per_mtok") is not None
                else None
            ),
        )

    plans = [one(entry) for entry in fitting]
    bound = sum(
        (
            plan.charge(min(incoming, target.context), request.max_tokens)
            for plan, target in zip(plans, fitting)
        ),
        0,
    )
    if bound > call_cap():
        raise LimitError("cost_budget", bound, call_cap(), "microEUR")
    primary = replace(
        plans[0],
        reserved=bound,
        attempt_bound=plans[0].charge(
            min(incoming, fitting[0].context), request.max_tokens
        ),
    )
    if len(plans) > 1:
        primary = replace(
            primary,
            fallback=replace(
                plans[1],
                attempt_bound=plans[1].charge(
                    min(incoming, fitting[1].context), request.max_tokens
                ),
            ),
        )
    return primary


def prepare(request: Request) -> Plan:
    policy = ServerlessPolicy()
    model = policy.models["code"]
    body = request.model_dump(exclude_none=True, by_alias=True)
    if "response_format" not in request.model_fields_set:
        body.pop("response_format", None)
    for message in body["messages"]:
        if not message["tool_calls"]:
            message.pop("tool_calls")
    body["messages"].insert(
        0, {"role": "system", "content": Path("prompts/code-language.txt").read_text()}
    )
    body.update(
        model=model,
        max_tokens=request.max_tokens,
        temperature=request.temperature,
        reasoning_effort=request.reasoning_effort,
        stream=True,
        stream_options={"include_usage": True},
    )
    if not request.tools or request.tool_choice == "none":
        for key in ("tools", "tool_choice", "parallel_tool_calls"):
            body.pop(key, None)
    if os.environ.get("ATLAS_PROVIDER"):
        body = provider_settings.request_options(body)
    incoming = len(json.dumps(body, ensure_ascii=False).encode()) + 512
    context = policy.config["code"]["capabilities"][model]["context"]
    retry_margin = 512 if request.validate_output else 0
    if incoming + retry_margin + request.max_tokens > context:
        raise LimitError(
            "context_exceeded",
            incoming + retry_margin + request.max_tokens,
            context,
            "tokens",
        )
    preset = bool(os.environ.get("ATLAS_PROVIDER"))
    endpoint = (
        provider_settings.endpoint()
        if preset
        else os.environ["SCW_GENERATIVE_BASE_URL"].rstrip("/")
    )
    if not preset and not endpoint.startswith("https://"):
        raise ValueError("provider_configuration")

    def priced_plan(role: str) -> Plan:
        selected_model = policy.models[role]
        context = policy.config[role]["capabilities"][selected_model]["context"]
        if incoming + retry_margin + request.max_tokens > context:
            raise LimitError(
                "context_exceeded",
                incoming + retry_margin + request.max_tokens,
                context,
                "tokens",
            )
        price = policy.prices[selected_model]
        return Plan(
            json.dumps(dict(body, model=selected_model), ensure_ascii=False),
            endpoint,
            0,
            Decimal(str(price["input_eur_per_mtok"])),
            Decimal(str(price["output_eur_per_mtok"])),
            unpriced=bool(price.get("unpriced", False)),
        )

    plan = priced_plan("code")
    fallback = (
        priced_plan("text_fallback")
        if preset and "text_fallback" in policy.models
        else None
    )
    unpriced = plan.unpriced or bool(fallback and fallback.unpriced)
    if unpriced:
        # An unpriced route has no defensible monetary bound, including recovery.
        plan = replace(
            plan, input_rate=Decimal(0), output_rate=Decimal(0), unpriced=True
        )
        if fallback:
            fallback = replace(
                fallback, input_rate=Decimal(0), output_rate=Decimal(0), unpriced=True
            )
    bound = plan.charge(incoming + retry_margin, request.max_tokens)
    plan = replace(plan, attempt_bound=bound)
    if fallback:
        fallback_bound = fallback.charge(incoming + retry_margin, request.max_tokens)
        fallback = replace(fallback, attempt_bound=fallback_bound)
        bound += fallback_bound
    reserved = bound * (2 if request.validate_output else 1)
    if not preset:
        reserved = plan.charge(incoming, request.max_tokens)
        if request.validate_output:
            reserved += plan.charge(incoming + retry_margin, request.max_tokens)
    if reserved > call_cap() or (not preset and reserved <= 0):
        raise LimitError("cost_budget", reserved, call_cap(), "microEUR")
    return replace(plan, reserved=reserved, fallback=fallback)


def call_cap() -> int:
    """Per-call reservation ceiling in microEUR (default 0.05 EUR).

    Coding clients such as Codex send large first requests; operators may raise
    the ceiling with ATLAS_RAW_CALL_MAX_MICRO_EUR (bounded to 5 EUR). Key, user
    and instance budgets still apply on top of it.
    """
    try:
        value = int(os.environ.get("ATLAS_RAW_CALL_MAX_MICRO_EUR", "50000"))
    except ValueError:
        return 50000
    return min(max(value, 1), 5000000)


def raw_models() -> dict[str, str]:
    """Public raw IDs for configured chat roles; embeddings have no chat endpoint."""
    policy = ServerlessPolicy(allow_unpriced=True)
    return {
        "raw/" + model: role
        for role, model in policy.models.items()
        if role != "embedding"
        and "context" in policy.config[role]["capabilities"][model]
    }


def client_raw_model(name: str) -> str:
    """Resolve client aliases without allowing arbitrary provider model selection."""
    configured = raw_models()
    if name in configured:
        return name
    default = os.environ.get("ATLAS_API_DEFAULT_RAW_MODEL") or (
        "raw/" + ServerlessPolicy(allow_unpriced=True).models["code"]
    )
    if default not in configured:
        raise ValueError("invalid_default_raw_model")
    return default


def prepare_raw(request: Any) -> Plan:
    """Prepare a direct configured-model request without an Atlas prompt or fallback."""
    policy = ServerlessPolicy(allow_unpriced=True)
    roles = raw_models()
    role = roles.get(request.model)
    if role is None:
        raise ValueError("invalid_model")
    model = policy.models[role]
    price = policy.prices.get(model)
    if price is None or price.get("unpriced", False):
        raise LimitError("unpriced_model", 0, 0, "microEUR")
    body = request.model_dump(exclude_none=True, by_alias=True)
    body.update(model=model, stream=True, stream_options={"include_usage": True})
    incoming = len(json.dumps(body, ensure_ascii=False).encode()) + 512
    images = sum(
        1
        for message in body["messages"]
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if isinstance(part, dict) and part.get("type") == "image_url"
    )
    incoming += images * 16384
    if images and not policy.config[role]["capabilities"][model].get("vision"):
        raise ValueError("missing_capabilities")
    context = policy.config[role]["capabilities"][model]["context"]
    if incoming + request.max_tokens > context:
        raise LimitError(
            "context_exceeded", incoming + request.max_tokens, context, "tokens"
        )
    plan = Plan(
        json.dumps(body, ensure_ascii=False),
        provider_settings.endpoint()
        if os.environ.get("ATLAS_PROVIDER")
        else os.environ["SCW_GENERATIVE_BASE_URL"].rstrip("/"),
        0,
        Decimal(str(price["input_eur_per_mtok"])),
        Decimal(str(price["output_eur_per_mtok"])),
    )
    reserved = plan.charge(incoming, request.max_tokens)
    if reserved <= 0 or reserved > call_cap():
        raise LimitError("cost_budget", reserved, call_cap(), "microEUR")
    return replace(plan, reserved=reserved, attempt_bound=reserved)


def retry(plan: Plan, error: str, remaining: int) -> Plan:
    body = json.loads(plan.body)
    body["messages"].append(
        {
            "role": "user",
            "content": "Previous output failed validation: "
            + error[:80]
            + ". Produce a nonempty answer in the requested response format; JSON must satisfy the supplied schema.",
        }
    )
    incoming = len(json.dumps(body, ensure_ascii=False).encode()) + 512
    reserved = plan.charge(incoming, body["max_tokens"])
    fallback = None
    if plan.fallback:
        fallback_body = dict(body, model=json.loads(plan.fallback.body)["model"])
        fallback = replace(
            plan.fallback, body=json.dumps(fallback_body, ensure_ascii=False)
        )
        reserved += fallback.charge(incoming, body["max_tokens"])
    if reserved > remaining:
        raise LimitError("cost_budget", reserved, remaining, "microEUR")
    return replace(
        plan,
        body=json.dumps(body, ensure_ascii=False),
        reserved=reserved,
        fallback=fallback,
    )


class Usage(BaseModel):
    prompt_tokens: int = Field(ge=0, le=1048576, strict=True)
    completion_tokens: int = Field(ge=0, le=8192, strict=True)
    prompt_tokens_details: dict[str, int] | None = None


class Choice(BaseModel):
    index: Literal[0]
    delta: dict[str, Any]
    finish_reason: Literal["stop", "tool_calls", "length"] | None = None


class Frame(BaseModel):
    error: None = None
    choices: list[Choice] = Field(max_length=1)
    usage: Usage | None = None


async def transport(plan: Plan) -> AsyncGenerator[bytes, None]:
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60 if plan.fallback else 120),
            trust_env=False,
        ) as session:
            async with session.post(
                plan.endpoint + "/chat/completions",
                data=plan.body.encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer "
                    + (plan.api_key or provider_settings.api_key()),
                },
                allow_redirects=False,
            ) as response:
                if response.status == 429 or response.status >= 500:
                    raise TransientProviderError("provider_error")
                if response.status != 200:
                    raise RuntimeError("provider_error")
                async for piece in response.content.iter_chunked(16384):
                    yield piece
    except (aiohttp.ClientError, TimeoutError):
        raise TransientProviderError("provider_error") from None


async def attempt_events(plan: Plan) -> AsyncGenerator[dict[str, Any], None]:
    from contextlib import aclosing

    buffer, total, done, finished = b"", 0, False, False
    usage: Usage | None = None
    async with aclosing(transport(plan)) as chunks:
        async for piece in chunks:
            total += len(piece)
            if total > 2_000_000:
                raise LimitError("provider_stream_limit", total, 2_000_000, "bytes")
            buffer += piece
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line or line.startswith(b":"):
                    continue
                if done or not line.startswith(b"data:"):
                    raise ValueError("invalid_provider_stream")
                data = line[5:].strip()
                if data == b"[DONE]":
                    done = True
                    continue
                frame = Frame.model_validate(json.loads(data))
                if frame.usage is not None:
                    if os.environ.get("ATLAS_PROVIDER"):
                        validate_usage(
                            frame.usage.model_dump(),
                            usage.model_dump() if usage is not None else None,
                        )
                        maximum = json.loads(plan.body)["max_tokens"]
                        if frame.usage.completion_tokens > maximum:
                            raise LimitError(
                                "output_budget",
                                frame.usage.completion_tokens,
                                maximum,
                                "tokens",
                            )
                    elif usage is not None:
                        raise ValueError("duplicate_usage")
                    usage = frame.usage
                for choice in frame.choices:
                    if finished:
                        raise ValueError("delta_after_finish")
                    finished = choice.finish_reason is not None
                    # Routed and preset providers may stream typed content parts
                    # (text, reasoning, references); keep only the answer text.
                    if (
                        os.environ.get("ATLAS_PROVIDER") or plan.provider
                    ) and "content" in choice.delta:
                        choice.delta["content"] = Delta.text_content(
                            choice.delta["content"]
                        )
                if frame.choices:
                    yield {"choices": [c.model_dump() for c in frame.choices]}
    if buffer.strip() or not done or not finished or usage is None:
        raise ValueError("incomplete_provider_stream")
    yield {
        "choices": [],
        "usage": usage.model_dump(exclude_none=True),
        "cost_micro_eur": plan.charge(
            usage.prompt_tokens,
            usage.completion_tokens,
            (usage.prompt_tokens_details or {}).get("cached_tokens", 0),
        ),
        "served_by": plan.provider + "/" + plan.model if plan.provider else None,
    }


async def events(plan: Plan) -> AsyncGenerator[dict[str, Any], None]:
    from contextlib import aclosing

    emitted = False
    try:
        async with aclosing(attempt_events(plan)) as frames:
            async for frame in frames:
                emitted = True
                yield frame
        return
    except TransientProviderError:
        if emitted or plan.fallback is None:
            raise
    logging.getLogger(__name__).warning("devapi_transient_fallback")
    async with aclosing(attempt_events(plan.fallback)) as frames:
        async for frame in frames:
            if "cost_micro_eur" in frame:
                # Failed primary usage is unknown: keep its conservative bound.
                frame["cost_micro_eur"] += plan.attempt_bound
            yield frame
