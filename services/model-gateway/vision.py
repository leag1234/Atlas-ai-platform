"""M12 sovereign vision with a conservative reservation before any paid call."""

from packages.limits import LimitError, ProviderLimitError

from packages.profiles import PROFILES

import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Literal, Self
from packages.language import conversation_language, language_instruction

import aiohttp
import yaml
from pydantic import Field, model_validator

from agent_provider import Completion
from packages.images import VisionInput
from packages.context_limit import ContextExceeded


class VisionRequest(VisionInput):
    lang: Literal["fr", "de", "es", "it", "en"] = "fr"
    timeout: float = Field(gt=0, le=300, default=120)
    profile: str | None = Field(default=None, exclude_if=lambda value: value is None)
    max_tokens: int | None = Field(
        default=None, ge=1, le=16000, exclude_if=lambda value: value is None
    )

    @property
    def budget(self) -> Decimal:
        return Decimal("0.10" if self.profile is not None else "0.05")

    @model_validator(mode="after")
    def deadline_limit(self) -> Self:
        if self.profile is not None and self.profile not in PROFILES:
            raise ValueError("invalid_profile")
        if self.timeout > 120:
            raise LimitError("timeout_budget", int(self.timeout * 1000), 120000, "ms")
        return self


class VisionProvider:
    def __init__(self) -> None:
        self.config = yaml.safe_load(
            Path(__file__).with_name("vision.yaml").read_text()
        )
        self.input_price = Decimal(str(self.config["input_eur_per_mtok"]))
        self.output_price = Decimal(str(self.config["output_eur_per_mtok"]))
        if any(
            not p.is_finite() or p < 0 for p in (self.input_price, self.output_price)
        ):
            raise ValueError("missing_price")

    def cost(self, incoming: int, outgoing: int) -> Decimal:
        return (incoming * self.input_price + outgoing * self.output_price) / 1000000

    def reserve(self, request: VisionRequest) -> tuple[int, Decimal]:
        images = [im for m in request.messages for im in m.images]
        if not images:
            raise ValueError("image_required")
        # One token per UTF-8 byte bounds text; include message framing and image
        # row/end markers. Native 16x16 patches bound any provider downscaling.
        tokens = 1024 + sum(len(m.text.encode()) + 64 for m in request.messages)
        tokens += sum(
            ((int(im["width"]) + 15) // 16 + 1) * ((int(im["height"]) + 15) // 16) + 1
            for im in images
        )
        if tokens + self.config["max_tokens"] > self.config["context_tokens"]:
            raise ContextExceeded(
                tokens + self.config["max_tokens"], self.config["context_tokens"]
            )
        reservation = self.cost(tokens, self.config["max_tokens"])
        if reservation > request.budget:
            raise ProviderLimitError(
                "cost_budget",
                int(reservation * 1000000),
                int(request.budget * 1000000),
                "microEUR",
            )
        return tokens, reservation

    async def complete(self, payload: object) -> dict[str, object]:
        started = monotonic()
        request = VisionRequest.model_validate(payload)
        incoming, reserved = self.reserve(request)
        prompt = Path("prompts/chat.txt").read_text()
        prompt += Path("prompts/vision.txt").read_text()
        if sum(len(message.images) for message in request.messages) > 1:
            prompt += Path("prompts/vision-multiple.txt").read_text()
        pixels = [
            {"image": index, "rgb": image["uniform_rgb"]}
            for index, image in enumerate(
                (im for message in request.messages for im in message.images), 1
            )
            if "uniform_rgb" in image
        ]
        if pixels:
            prompt += (
                Path("prompts/vision-pixels.txt")
                .read_text()
                .format(measurements=json.dumps(pixels))
            )
        prompt += language_instruction(
            conversation_language(request.messages, request.lang)
        )
        # Count the versioned system instruction before transport as well.
        incoming += len(prompt.encode()) + 64
        reserved = self.cost(incoming, self.config["max_tokens"])
        if incoming + self.config["max_tokens"] > self.config["context_tokens"]:
            raise ContextExceeded(
                incoming + self.config["max_tokens"], self.config["context_tokens"]
            )
        if reserved > request.budget:
            raise ProviderLimitError(
                "cost_budget",
                int(reserved * 1000000),
                int(request.budget * 1000000),
                "microEUR",
            )
        messages = [{"role": "system", "content": prompt}]
        messages.extend(m.model_dump() for m in request.messages)
        if request.profile is not None:
            # Full-pixel reservation already covers high detail; do not let a
            # provider's automatic downsampling discard identification evidence.
            for message in messages:
                content = message.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "image_url":
                            part["image_url"].setdefault("detail", "high")
        body = {
            "model": self.config["model"],
            "messages": messages,
            "max_tokens": self.config["max_tokens"],
            "temperature": 0,
            "reasoning_effort": "none",
        }
        remaining = request.timeout - (monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("timeout")
        if request.profile is not None:
            from agent_provider import AgentProvider, AgentRequest
            from quality import complete

            options: dict[str, object] = {
                "messages": messages,
                "tools": [],
                "timeout": remaining,
                "local_enabled": False,
                "observe": True,
                "profile": request.profile,
            }
            if request.max_tokens is not None:
                options["max_tokens"] = request.max_tokens
            answer = await complete(
                AgentProvider(), AgentRequest.model_validate(options)
            )
            if not answer.get("text") or answer.get("calls"):
                raise RuntimeError("provider_response_invalid")
            return answer
        async with asyncio.timeout(remaining):
            data = await self.post(body, remaining)
        result = Completion.model_validate(data)
        choice, usage = result.choices[0], result.usage
        if (
            choice.finish_reason != "stop"
            or not choice.message.content
            or choice.message.tool_calls
            or usage.prompt_tokens > incoming
            or usage.completion_tokens > self.config["max_tokens"]
        ):
            raise RuntimeError("provider_response_invalid")
        cost = self.cost(usage.prompt_tokens, usage.completion_tokens)
        if cost > reserved:
            raise RuntimeError("provider_response_invalid")
        return {
            "text": choice.message.content,
            "usage": usage.model_dump(),
            "cost_eur": str(cost),
            "reserved_eur": str(reserved),
            "observation": {"provider": "escalade", "route": "complexe"},
        }

    async def post(self, body: dict[str, object], timeout: float) -> object:
        endpoint = os.environ["SCW_GENERATIVE_BASE_URL"].rstrip("/")
        if not endpoint.startswith("https://"):
            raise ValueError("provider_configuration")
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
            ) as client:
                async with client.post(
                    endpoint + "/chat/completions",
                    json=body,
                    headers={
                        "Authorization": "Bearer "
                        + os.environ["SCW_GENERATIVE_API_KEY"]
                    },
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise RuntimeError("provider_error")
                    data = bytearray()
                    async for piece in response.content.iter_chunked(16384):
                        data.extend(piece)
                        if len(data) > 800000:
                            raise ProviderLimitError(
                                "provider_response_limit", len(data), 800000, "bytes"
                            )
            return json.loads(data)
        except LimitError:
            raise
        except (aiohttp.ClientError, ValueError):
            raise RuntimeError("provider_error") from None
