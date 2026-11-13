"""Gateway streaming boundary, invoked only after the harness reservation."""

from packages.limits import LimitError
from packages.provider_calls import mark_started
from packages import privacy

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
import json

import aiohttp

Sink = Callable[[dict[str, object]], Awaitable[None]]
metadata_only_context: ContextVar[bool] = ContextVar("metadata_only", default=False)
sink_context: ContextVar[Sink | None] = ContextVar("stream_sink", default=None)


def provider_credit_detail(status: object) -> str:
    # D5 wording of an exhausted provider account, with the provider's status.
    suffix = (
        f" (provider status {status})"
        if type(status) is int and 100 <= status <= 599
        else ""
    )
    return (
        "The model provider refused the request: account credit exhausted"
        + suffix
        + "."
    )


async def receive(
    url: str, payload: dict[str, object], timeout: float, sink: Sink
) -> dict[str, object]:
    mark_started()
    total = 0
    result: dict[str, object] | None = None
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            trust_env=False,
            read_bufsize=800000,
        ) as client:
            async with client.post(
                url, json=payload, allow_redirects=False, headers=privacy.headers()
            ) as response:
                if response.status != 200:
                    raise RuntimeError("gateway_error")
                async for line in response.content:
                    total += len(line)
                    if total > (8000000 if payload.get("profile") else 1000000):
                        raise LimitError(
                            "stream_limit",
                            total,
                            8000000 if payload.get("profile") else 1000000,
                            "bytes",
                        )
                    if not line.strip():
                        continue
                    if not line.startswith(b"data:") or result is not None:
                        raise ValueError("invalid_stream")
                    event = json.loads(line[5:])
                    if not isinstance(event, dict):
                        raise ValueError("invalid_stream")
                    if event.get("error") == privacy.CODE:
                        from services.orchestrator.loop import StrictPrivacyRefusal

                        raise StrictPrivacyRefusal(
                            str(event.get("capability") or "inference")[:64]
                        )
                    if event.get("error") == "provider_failure":
                        from services.orchestrator.model import GatewayError

                        raise GatewayError(
                            "provider_error",
                            502,
                            str(event.get("message", "provider_error")),
                            retries=int(event.get("retries", 0)),
                        )
                    if event.get("error") == "provider_quota":
                        from services.orchestrator.model import GatewayError

                        # D5: an exhausted provider account is not an outage.
                        raise GatewayError(
                            "provider_quota",
                            402,
                            provider_credit_detail(event.get("provider_status")),
                        )
                    if (
                        event.get("error") == "measured_limit"
                        and all(
                            type(event.get(k)) is int and event[k] >= 0
                            for k in ("measured", "limit")
                        )
                        and isinstance(event.get("unit"), str)
                        and isinstance(event.get("code"), str)
                    ):
                        if event["code"] == "cost_budget":
                            from services.orchestrator.model import GatewayError

                            ceiling = (
                                "0.30"
                                if payload.get("has_attachments")
                                or payload.get("produces_files")
                                else "0.10"
                            )
                            raise GatewayError(
                                "cost_budget",
                                429,
                                f"Cost reservation: {event['measured']} microEUR; remaining {event['limit']} microEUR; request ceiling {ceiling} EUR",
                            )
                        raise LimitError(
                            event["code"],
                            event["measured"],
                            event["limit"],
                            event["unit"],
                        )
                    if event.get("error") == "context_exceeded" and all(
                        type(event.get(k)) is int and event[k] >= 0
                        for k in ("tokens", "limit")
                    ):
                        from services.orchestrator.model import GatewayError

                        raise GatewayError(
                            "context_exceeded",
                            413,
                            f"Conversation: {event['tokens']} tokens, limit {event['limit']} tokens",
                        )
                    if set(event) == {"result"} and isinstance(event["result"], dict):
                        result = event["result"]
                    elif set(event) == {"phase"} and event["phase"] in {
                        "reasoning_fallback",
                        "provider_fallback",
                    }:
                        await sink(event)
                    elif set(event) == {"delta"} and isinstance(event["delta"], dict):
                        delta = event["delta"]
                        if not delta or not set(delta) <= {
                            "content",
                            "reasoning_content",
                        }:
                            raise ValueError("invalid_delta")
                        if any(not isinstance(v, str) for v in delta.values()):
                            raise ValueError("invalid_delta")
                        await sink(event)
                    else:
                        raise ValueError("gateway_stream_error")
        if result is None:
            raise ValueError("incomplete_stream")
        return result
    except aiohttp.ClientError:
        raise RuntimeError("gateway_unavailable") from None
