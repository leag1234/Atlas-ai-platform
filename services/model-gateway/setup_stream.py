"""Bounded standard-library streaming probe for the dependency-free installer."""

import json
from time import monotonic
from typing import BinaryIO

from stream_usage import validate_usage


def read_completion(response: BinaryIO) -> dict[str, str]:
    message = {"content": "", "reasoning_content": ""}
    usage: dict[str, object] | None = None
    response_id: str | None = None
    finish: str | None = None
    done = False
    total = 0
    started = monotonic()
    while line := response.readline(800001):
        total += len(line)
        if total > 800000 or monotonic() - started > 60:
            raise ValueError("stream_limit")
        line = line.rstrip(b"\r\n")
        if not line or line.startswith(b":"):
            continue
        if done or not line.startswith(b"data:"):
            raise ValueError("invalid_sse_field_or_trailing_data")
        data = line[5:].strip()
        if data == b"[DONE]":
            if finish is None or usage is None:
                raise ValueError("incomplete_stream")
            done = True
            continue
        frame = json.loads(data)
        if not isinstance(frame, dict) or frame.get("error") is not None:
            raise ValueError("invalid_frame")
        identifier = frame.get("id")
        if identifier is not None and not isinstance(identifier, str):
            raise ValueError("invalid_provider_id")
        if identifier:
            if response_id and identifier != response_id:
                raise ValueError("provider_id_changed")
            response_id = identifier
        current = frame.get("usage")
        if current is not None:
            if not isinstance(current, dict):
                raise ValueError("invalid_usage")
            validate_usage(current, usage)
            if current["completion_tokens"] > 1024:
                raise ValueError("output_budget")
            usage = current
        choices = frame.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise ValueError("invalid_choices")
        for choice in choices:
            if finish is not None:
                raise ValueError("delta_after_finish")
            if not isinstance(choice, dict) or choice.get("index") != 0:
                raise ValueError("invalid_choice")
            delta = choice.get("delta")
            if not isinstance(delta, dict) or delta.get("tool_calls"):
                raise ValueError("invalid_delta")
            for field in ("content", "reasoning_content", "reasoning"):
                value = delta.get(field)
                if value is not None and not isinstance(value, str):
                    raise ValueError("invalid_delta")
            message["content"] += delta.get("content") or ""
            message["reasoning_content"] += (
                delta.get("reasoning_content") or delta.get("reasoning") or ""
            )
            finish = choice.get("finish_reason")
            if finish not in (None, "stop", "length"):
                raise ValueError("invalid_finish")
    if not done:
        raise ValueError("incomplete_stream")
    return message
