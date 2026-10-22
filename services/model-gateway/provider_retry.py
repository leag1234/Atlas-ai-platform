"""Transient retries keep unknown charges reserved and respect one deadline."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from decimal import Decimal
import json
import logging
from time import monotonic

from agent_provider import AgentRequest, Usage
from packages.limits import ProviderLimitError
from serverless import ServerlessPolicy


class TransientProviderError(RuntimeError):
    def __init__(
        self, code: str, *, usage: Usage | None = None, finish_reason: str | None = None
    ) -> None:
        super().__init__(code)
        self.usage = usage
        self.finish_reason = finish_reason


@dataclass
class RetryLedger:
    spent: Decimal = Decimal(0)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated: bool = False


async def retry_stream(
    call: Callable[[AgentRequest, str, float], AsyncGenerator[dict[str, object], None]],
    request: AgentRequest,
    model: str,
    timeout: float,
    allowance: Decimal,
    incoming: int,
    ledger: RetryLedger,
) -> AsyncGenerator[dict[str, object], None]:
    policy = ServerlessPolicy()
    reserved = policy.cost(model, incoming, request.max_tokens)
    started = monotonic()
    for index in range(4):
        if ledger.spent + reserved > allowance:
            raise ProviderLimitError(
                "cost_budget",
                int((ledger.spent + reserved) * 1000000),
                int(allowance * 1000000),
                "microEUR",
            )
        remaining = timeout if index == 0 else timeout - (monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("timeout")
        visible = False
        try:
            async with aclosing(call(request, model, remaining)) as events:
                async for event in events:
                    visible |= "delta" in event
                    yield event
            return
        except TransientProviderError as exc:
            # The outer gateway already reserves the final failed attempt.
            if visible or index == 3:
                raise
            delay = (2, 5, 15)[index]
            if timeout - (monotonic() - started) <= delay:
                raise TimeoutError("timeout") from None
            prompt, output = incoming, request.max_tokens
            if exc.usage is not None:
                prompt, output = exc.usage.prompt_tokens, exc.usage.completion_tokens
                if prompt > incoming or output > request.max_tokens:
                    raise RuntimeError("provider_usage_exceeds_reservation") from None
            else:
                ledger.estimated = True
            ledger.spent += policy.cost(model, prompt, output)
            ledger.prompt_tokens += prompt
            ledger.completion_tokens += output
            logging.getLogger(__name__).warning(
                json.dumps(
                    {
                        "event": "provider_transient_retry",
                        "retry": index + 1,
                        "delay_seconds": delay,
                        "reserved_eur": str(ledger.spent),
                        "usage_measured": exc.usage is not None,
                    }
                )
            )
            await asyncio.sleep(delay)
