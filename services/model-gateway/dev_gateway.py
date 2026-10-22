"""M15 single-attempt code transport with a frozen, conservatively priced request."""

from packages.limits import LimitError

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import json
import os
from pathlib import Path
from typing import Any, Literal

import aiohttp
from pydantic import BaseModel, Field
from packages.dev_request import Request
from serverless import ServerlessPolicy


@dataclass(frozen=True)
class Plan:
    body: str
    endpoint: str
    reserved: int
    input_rate: Decimal
    output_rate: Decimal

    def charge(self, incoming: int, outgoing: int) -> int:
        return int(
            (
                incoming * self.input_rate + outgoing * self.output_rate
            ).to_integral_value(rounding=ROUND_CEILING)
        )


def prepare(request: Request) -> Plan:
    policy = ServerlessPolicy()
    model = policy.models["code"]
    body = request.model_dump(exclude_none=True)
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
    if not request.tools:
        for key in ("tools", "tool_choice", "parallel_tool_calls"):
            body.pop(key, None)
    incoming = len(json.dumps(body, ensure_ascii=False).encode()) + 512
    context = policy.config["code"]["capabilities"][model]["context"]
    if incoming + request.max_tokens > context:
        raise LimitError(
            "context_exceeded", incoming + request.max_tokens, context, "tokens"
        )
    price = policy.prices[model]
    endpoint = os.environ["SCW_GENERATIVE_BASE_URL"].rstrip("/")
    if not endpoint.startswith("https://"):
        raise ValueError("provider_configuration")
    plan = Plan(
        json.dumps(body, ensure_ascii=False),
        endpoint,
        0,
        Decimal(str(price["input_eur_per_mtok"])),
        Decimal(str(price["output_eur_per_mtok"])),
    )
    reserved = plan.charge(incoming, request.max_tokens)
    if not 0 < reserved <= 50000:
        raise LimitError("cost_budget", reserved, 50000, "microEUR")
    return Plan(plan.body, plan.endpoint, reserved, plan.input_rate, plan.output_rate)


class Usage(BaseModel):
    prompt_tokens: int = Field(ge=0, le=1048576, strict=True)
    completion_tokens: int = Field(ge=0, le=8192, strict=True)


class Choice(BaseModel):
    index: Literal[0]
    delta: dict[str, Any]
    finish_reason: Literal["stop", "tool_calls", "length"] | None = None


class Frame(BaseModel):
    choices: list[Choice] = Field(max_length=1)
    usage: Usage | None = None


async def transport(plan: Plan) -> AsyncGenerator[bytes, None]:
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120), trust_env=False
        ) as session:
            async with session.post(
                plan.endpoint + "/chat/completions",
                data=plan.body.encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + os.environ["SCW_GENERATIVE_API_KEY"],
                },
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise RuntimeError("provider_error")
                async for piece in response.content.iter_chunked(16384):
                    yield piece
    except aiohttp.ClientError:
        raise RuntimeError("provider_error") from None


async def events(plan: Plan) -> AsyncGenerator[dict[str, Any], None]:
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
                    if usage is not None:
                        raise ValueError("duplicate_usage")
                    usage = frame.usage
                for choice in frame.choices:
                    if finished:
                        raise ValueError("delta_after_finish")
                    finished = choice.finish_reason is not None
                if frame.choices:
                    yield {"choices": [c.model_dump() for c in frame.choices]}
    if buffer.strip() or not done or not finished or usage is None:
        raise ValueError("incomplete_provider_stream")
    yield {
        "choices": [],
        "usage": usage.model_dump(),
        "cost_micro_eur": plan.charge(usage.prompt_tokens, usage.completion_tokens),
    }
