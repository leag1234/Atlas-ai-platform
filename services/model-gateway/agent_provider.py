"""Validated tool-calling transport and human-confirmed pricing, POC-P6."""

from packages.limits import LimitError, ProviderLimitError, MalformedToolCall

from packages.profiles import PROFILES

import asyncio
import json
import logging
import re
from time import monotonic
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Self
from collections.abc import AsyncGenerator

import aiohttp
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator, ValidationError
from packages.validation import numeric_limit, malformed_tool_name


def classify(messages: list[dict[str, object]]) -> str:
    # Conservative allowlist: only short, familiar transformations use S.
    simple = r"(?i)\b(tradui\w*|translate|reformul\w*|formuliere|rewrite|corrig\w*|summary|résumer|abkürzung|bedeutet|significa|capital|come si dice|che ore|auguri|asunto|ideen)\b"
    complex_task = r"(?i)\b(analys\w*|anális\w*|analyz\w*|ris\w*|risk\w*|strat\w*|proof|prove|beweis\w*|dimostr\w*|design\w*|progett\w*|refactor\w*|algorithm\w*|algoritm\w*)\b"
    users = [m.get("content") for m in messages if m.get("role") == "user"]
    if not users or any(m.get("role") == "tool" for m in messages):
        return "reasoning"
    if all(
        isinstance(t, str)
        and 0 < len(t.strip()) <= 240
        and re.search(simple, t)
        and not re.search(complex_task, t)
        for t in users
    ):
        return "chat_simple"
    return "reasoning"


class Function(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    arguments: str = Field(max_length=16000)


class ToolCall(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    type: Literal["function"]
    function: Function


class Reply(BaseModel):
    content: str | None = Field(default=None, max_length=128000)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=10)


class Choice(BaseModel):
    finish_reason: Literal["stop", "tool_calls", "length"]
    message: Reply


class Usage(BaseModel):
    prompt_tokens: int = Field(ge=0, strict=True)
    completion_tokens: int = Field(ge=0, le=16000, strict=True)


class Completion(BaseModel):
    # Some providers return HTTP 200 for errors; never ignore one beside choices.
    error: None = None
    usage: Usage
    choices: list[Choice] = Field(min_length=1, max_length=1)


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    messages: list[dict[str, object]] = Field(min_length=1, max_length=100)
    tools: list[dict[str, object]] = Field(min_length=0, max_length=8)
    tool_choice: Literal["auto", "none"] | dict[str, object] = Field(
        default="auto", exclude_if=lambda value: value == "auto"
    )
    timeout: float = Field(gt=0, le=300)
    local_enabled: bool = True
    observe: bool = False
    profile: str | None = Field(default=None, exclude_if=lambda value: value is None)
    max_tokens: int = Field(
        default=2048, ge=1, le=16000, exclude_if=lambda value: value == 2048
    )
    reasoning_effort: Literal["none", "low", "high"] = Field(
        default="none", exclude_if=lambda value: value == "none"
    )
    has_attachments: bool = Field(
        default=False, exclude_if=lambda value: value is False
    )
    produces_files: bool = Field(default=False, exclude_if=lambda value: value is False)
    budget_eur: str | None = Field(
        default=None, max_length=40, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def quality(self) -> Self:
        if isinstance(self.tool_choice, dict):
            function = self.tool_choice.get("function")
            if (
                set(self.tool_choice) != {"type", "function"}
                or self.tool_choice.get("type") != "function"
                or not isinstance(function, dict)
                or set(function) != {"name"}
                or function.get("name") not in {"terminal_command", "publish_document"}
                or not any(
                    declared.get("name") == function["name"]
                    for tool in self.tools
                    if isinstance(declared := tool.get("function"), dict)
                )
            ):
                raise ValueError("invalid_required_file_tool")
        if self.profile is not None and self.profile not in PROFILES:
            raise ValueError("invalid_profile")
        deadline = (
            300
            if (self.has_attachments or self.produces_files) and self.profile
            else 120
        )
        if self.timeout > deadline:
            raise LimitError(
                "timeout_budget", int(self.timeout * 1000), deadline * 1000, "ms"
            )
        if self.profile is None:
            if (
                self.has_attachments
                or self.produces_files
                or self.max_tokens != 2048
                or self.budget_eur is not None
                or self.reasoning_effort == "high"
            ):
                raise ValueError("invalid_legacy_profile")
            self.reasoning_effort = "none"
            return self
        ceiling = 6000
        if "max_tokens" not in self.model_fields_set:
            self.max_tokens = ceiling
        if self.max_tokens > ceiling:
            raise LimitError("output_budget", self.max_tokens, ceiling, "tokens")
        self.reasoning_effort = "none"
        cap = Decimal(
            "0.30" if (self.has_attachments or self.produces_files) else "0.10"
        )
        try:
            budget = Decimal(self.budget_eur) if self.budget_eur is not None else cap
        except InvalidOperation:
            raise ValueError("cost_budget") from None
        if not budget.is_finite():
            raise ValueError("cost_budget: expected a finite monetary amount")
        if not 0 <= budget <= cap:
            raise LimitError(
                "cost_budget", int(budget * 1000000), int(cap * 1000000), "microEUR"
            )
        return self


class AgentProvider:
    def __init__(self) -> None:
        self.model = os.environ["ESCALATION_MODEL"]
        routing = yaml.safe_load(Path(__file__).with_name("routing.yaml").read_text())
        self.local_endpoint = os.path.expandvars(
            routing["providers"]["local"]["base_url"]
        ).rstrip("/")
        self.local_model = os.path.expandvars(
            routing["task_classes"]["chat_simple"]["primary"]["model"]
        )
        catalogue = yaml.safe_load(Path(__file__).with_name("pricing.yaml").read_text())
        if self.model not in catalogue["models"]:
            raise ValueError("missing_price")
        self.price = catalogue["models"][self.model]
        for key in ("input_eur_per_mtok", "output_eur_per_mtok"):
            value = Decimal(str(self.price[key]))
            if not value.is_finite() or value < 0:
                raise ValueError("invalid_price")

    def configuration(self, local_enabled: bool = True) -> dict[str, object]:
        if not local_enabled:
            from serverless import ServerlessPolicy

            return ServerlessPolicy().configuration()
        return {
            key: str(self.price[key])
            for key in ("input_eur_per_mtok", "output_eur_per_mtok")
        } | {"max_tokens": 2048}

    async def stream(self, payload: object) -> AsyncGenerator[dict[str, object], None]:
        from stream_transport import stream

        from contextlib import aclosing

        async with aclosing(stream(self, payload)) as events:
            async for event in events:
                yield event

    async def complete(self, payload: object) -> dict[str, object]:
        request = AgentRequest.model_validate(payload)
        if request.profile is not None:
            from quality import complete

            return await complete(self, request)
        if not request.local_enabled:
            return await self.serverless_complete(request)
        endpoint = os.environ["SCW_GENERATIVE_BASE_URL"].rstrip("/")
        if not endpoint.startswith("https://"):
            raise ValueError("provider_configuration")
        started = monotonic()
        route = classify(request.messages)
        fallback = False

        def observed(answer: dict[str, object], provider: str) -> dict[str, object]:
            if request.observe:
                return {
                    **answer,
                    "observation": {
                        "provider": provider,
                        "route": "simple" if route == "chat_simple" else "complexe",
                    },
                }
            return answer

        if route == "chat_simple" and request.local_enabled:
            try:
                if "$" in self.local_model or "$" in self.local_endpoint:
                    raise RuntimeError("local_unconfigured")
                async with asyncio.timeout(min(2.0, request.timeout * 0.2)):
                    answer = await self._complete(
                        request,
                        self.local_endpoint,
                        self.local_model,
                        "",
                        min(2.0, request.timeout * 0.2),
                    )
                logging.getLogger(__name__).info(
                    json.dumps(
                        {
                            "event": "routing",
                            "route": route,
                            "provider": "local",
                            "fallback": False,
                        }
                    )
                )
                return observed(answer, "local")
            except (RuntimeError, TimeoutError):
                fallback = True
        remaining = request.timeout - (monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("timeout")
        logging.getLogger(__name__).info(
            json.dumps(
                {
                    "event": "routing",
                    "route": route,
                    "provider": "scaleway",
                    "fallback": fallback,
                }
            )
        )
        async with asyncio.timeout(remaining):
            answer = await self._complete(
                request,
                endpoint,
                self.model,
                os.environ["SCW_GENERATIVE_API_KEY"],
                remaining,
            )
            return observed(answer, "escalade")

    async def serverless_complete(self, request: AgentRequest) -> dict[str, object]:
        from serverless import ServerlessPolicy
        from provider_retry import RetryLedger, retry_stream
        from contextlib import aclosing

        policy = ServerlessPolicy()
        plan = policy.reserve(request.messages, request.tools)
        endpoint = os.environ["SCW_GENERATIVE_BASE_URL"].rstrip("/")
        if not endpoint.startswith("https://"):
            raise ValueError("provider_configuration")
        started = monotonic()
        unknown = Decimal(0)
        malformed_name: MalformedToolCall | None = None
        for index, model in enumerate((plan.primary, plan.fallback)):
            remaining = request.timeout - (monotonic() - started)
            if remaining <= 0:
                if malformed_name is not None:
                    raise MalformedToolCall(malformed_name.measured, index)
                raise TimeoutError("timeout")
            # Leave time for one fallback while keeping the original total deadline.
            allotted = remaining * 0.7 if index == 0 else remaining
            ledger = RetryLedger()

            async def completion_events(
                current: AgentRequest, selected: str, seconds: float
            ) -> AsyncGenerator[dict[str, object], None]:
                yield {
                    "result": await self._complete(
                        current,
                        endpoint,
                        selected,
                        os.environ["SCW_GENERATIVE_API_KEY"],
                        seconds,
                    )
                }

            try:
                async with (
                    asyncio.timeout(allotted),
                    aclosing(
                        retry_stream(
                            completion_events,
                            request,
                            model,
                            allotted,
                            plan.reserved_eur - unknown,
                            plan.incoming,
                            ledger,
                        )
                    ) as events,
                ):
                    async for event in events:
                        value = event["result"]
                        assert isinstance(value, dict)
                        answer = value
            except ProviderLimitError:
                raise
            except (RuntimeError, TimeoutError) as exc:
                if isinstance(exc, MalformedToolCall):
                    malformed_name = exc
                unknown += ledger.spent + (
                    plan.fallback_bound if index else plan.primary_bound
                )
                logging.getLogger(__name__).info(
                    json.dumps(
                        {
                            "event": "serverless_failure",
                            "task_type": plan.task_type,
                            "attempt": index + 1,
                            "reserved_unknown_eur": str(unknown),
                        }
                    )
                )
                if index:
                    if malformed_name is not None:
                        raise MalformedToolCall(
                            malformed_name.measured, index
                        ) from None
                    raise
                if isinstance(exc, MalformedToolCall):
                    request = request.model_copy(
                        update={"tools": [], "tool_choice": "none"}
                    )
                continue
            unknown += ledger.spent
            usage = answer.get("usage")
            if not isinstance(usage, dict):
                raise RuntimeError("provider_response_invalid")
            actual = policy.cost(
                model, int(usage["prompt_tokens"]), int(usage["completion_tokens"])
            )
            logging.getLogger(__name__).info(
                json.dumps(
                    {
                        "event": "serverless_usage",
                        "task_type": plan.task_type,
                        "fallback": bool(index),
                        "tokens": usage,
                        "cost_eur": str(actual),
                        "reserved_unknown_eur": str(unknown),
                    }
                )
            )
            answer["cost_eur"] = str(unknown + actual)
            if request.observe:
                answer["observation"] = {
                    "provider": "escalade",
                    "route": "simple"
                    if classify(request.messages) == "chat_simple"
                    else "complexe",
                    "task_type": plan.task_type,
                    "fallback": bool(index),
                }
            return answer
        raise RuntimeError("provider_error")

    async def _complete(
        self, request: AgentRequest, endpoint: str, model: str, key: str, timeout: float
    ) -> dict[str, object]:
        from provider_retry import TransientProviderError

        body = {
            "model": model,
            "messages": request.messages,
            "tools": request.tools,
            **({"tool_choice": request.tool_choice} if request.tools else {}),
            "max_tokens": 2048,
            "temperature": 0,
            "reasoning_effort": "none",
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
            ) as session:
                async with session.post(
                    endpoint + "/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer " + key} if key else {},
                    allow_redirects=False,
                ) as response:
                    if response.status in (502, 503, 504):
                        raise TransientProviderError("provider_error")
                    if response.status != 200:
                        raise RuntimeError("provider_error")
                    data = bytearray()
                    async for piece in response.content.iter_chunked(16384):
                        data.extend(piece)
                        if len(data) > 800000:
                            raise ProviderLimitError(
                                "provider_response_limit", len(data), 800000, "bytes"
                            )
            completion = Completion.model_validate(json.loads(data))
            if completion.usage.completion_tokens > 2048:
                raise ProviderLimitError(
                    "output_budget", completion.usage.completion_tokens, 2048, "tokens"
                )
            content_size = len(completion.choices[0].message.content or "")
            if content_size > 32000:
                raise ProviderLimitError(
                    "provider_response_limit", content_size, 32000, "characters"
                )
            if completion.choices[0].finish_reason == "length":
                raise ValueError("provider_response_invalid")
            reply = completion.choices[0].message
            if not (reply.content or "").strip() and not reply.tool_calls:
                raise TransientProviderError("provider_response_invalid")
            ids = [call.id for call in reply.tool_calls]
            if len(ids) != len(set(ids)):
                raise RuntimeError("provider_response_invalid")
            return {
                "text": reply.content or "",
                "usage": completion.usage.model_dump(),
                "calls": [
                    {
                        "id": call.id,
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    }
                    for call in reply.tool_calls
                ],
            }
        except LimitError:
            raise
        except json.JSONDecodeError:
            raise TransientProviderError("provider_error") from None
        except ValidationError as exc:
            malformed = malformed_tool_name(exc)
            if malformed is not None:
                raise malformed from None
            limit = numeric_limit(exc)
            if limit is not None:
                raise limit from None
            raise RuntimeError("provider_error") from None
        except (aiohttp.ClientError, ValueError):
            raise RuntimeError("provider_error") from None
