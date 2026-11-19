"""Gateway streaming boundary, invoked only after the harness reservation."""

from packages.limits import LimitError
from packages.provider_calls import mark_started
from packages import privacy
from packages.owned_server import parse as parse_owned

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
    calls = mark_started()
    total = 0
    result: dict[str, object] | None = None
    # Whether any answer text came back: a refusal after it may follow consumed
    # tokens, so only a refusal before the first delta settles the call at 0.
    delivered = False
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
                    from services.orchestrator.model import REFUSED_BEFORE_OUTPUT

                    if calls is not None and response.status in REFUSED_BEFORE_OUTPUT:
                        # Refused before the stream opened: nothing consumed.
                        calls.answered()
                        calls = None
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
                    if event.get("error") in privacy.CODES:
                        from services.orchestrator.loop import StrictPrivacyRefusal

                        raise StrictPrivacyRefusal(
                            str(event.get("capability") or "inference")[:64]
                        )
                    if event.get("error") == "owned_server" and (
                        answer := parse_owned(event)
                    ):
                        from services.orchestrator.loop import OwnedServerAnswer

                        # Starting, or a named refusal of its starter (D5).
                        raise OwnedServerAnswer(answer)
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
                        delivered = True
                        await sink(event)
                    else:
                        raise ValueError("gateway_stream_error")
        if result is None:
            raise ValueError("incomplete_stream")
        if calls is not None:
            # The stream ended with its result: the cost the gateway measured.
            calls.answered(result.get("cost_eur"))
        return result
    except aiohttp.ClientError:
        raise RuntimeError("gateway_unavailable") from None
    except Exception as exc:
        from services.orchestrator.model import refused_before_output

        if calls is not None and not delivered and refused_before_output(exc):
            # A refusal event before any answer text (privacy, owned server
            # starting, provider account exhausted, budget, context): nothing
            # was consumed. A provider failure (502) stays unknown.
            calls.answered()
        raise
