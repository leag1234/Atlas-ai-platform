"""Deterministic context accounting; observational only, never a budget decision."""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any

from packages.profiles import context_window

LAYERS = (
    "system",
    "project_instructions",
    "pinned_memory",
    "tool_declarations",
    "session_state",
    "recent_turns",
    "tool_results",
    "document_passages",
)
METHOD = "utf8_bytes_div_4_ceil_per_component"
samples: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "context_samples", default=None
)
parts: ContextVar[dict[str, dict[str, Any]] | None] = ContextVar(
    "context_parts", default=None
)


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def tokens(value: str) -> int:
    return (len(value.encode("utf-8")) + 3) // 4


def register(content: str, components: dict[str, Any]) -> str:
    """Register only server-built messages, never infer authority from user JSON."""
    current = parts.get()
    if current is not None:
        current[content] = components
    return content


def measure(
    messages: list[dict[str, object]], tools: object, profile: str | None
) -> dict[str, Any]:
    layers = dict.fromkeys(LAYERS, 0)
    registered = parts.get() or {}
    # Include serialized message framing in its own layer. For a mixed server
    # message, split its body by declared component; framing stays with history.
    for message in messages:
        content = message.get("content")
        components = registered.get(content) if isinstance(content, str) else None
        if components is not None:
            for layer, value in components.items():
                layers[layer] += tokens(encoded(value))
            layers["recent_turns"] += tokens(
                encoded({k: v for k, v in message.items() if k != "content"})
            )
        else:
            layer = (
                "system"
                if message.get("role") == "system"
                else "tool_results"
                if message.get("role") == "tool"
                else "recent_turns"
            )
            layers[layer] += tokens(encoded(message))
    layers["tool_declarations"] = tokens(encoded(tools)) if tools else 0
    return dict(
        method=METHOD,
        estimated=True,
        layers=layers,
        stable_prefix_tokens=sum(layers[k] for k in LAYERS[:4]),
        total_tokens=sum(layers.values()),
        model_window=context_window(profile) if profile else None,
        provider_input_tokens=None,
    )
