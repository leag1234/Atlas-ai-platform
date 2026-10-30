"""Versioned public API usage routing, kept separate from internal raw routes."""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


PUBLIC_MODELS = (
    "atlas-code",
    "atlas-chat",
    "atlas-code-2026-10-30",
    "atlas-chat-2026-10-30",
)
EFFORTS = ("none", "low", "medium", "high")


@dataclass(frozen=True)
class Target:
    provider: str
    model: str


def config() -> dict[str, Any]:
    value = json.loads(Path(__file__).with_name("api-routing.json").read_text())
    if not isinstance(value, dict):
        raise ValueError("invalid_api_routing")
    return value


def public_model(name: str) -> str:
    value = config()
    return str(value.get("aliases", {}).get(name, name))


def select_public_model(name: str) -> str:
    """Unknown client brand names retain the historic code-default behavior."""
    if name.startswith("raw/"):
        raise ValueError("invalid_model")
    return public_model(name) if name in PUBLIC_MODELS else "atlas-code"


def targets(name: str, effort: str) -> tuple[str, tuple[Target, ...]]:
    usage = public_model(name)
    entries = config().get("usages", {}).get(usage, {}).get(effort)
    if usage not in ("atlas-code", "atlas-chat") or not isinstance(entries, list):
        raise ValueError("invalid_model")
    selected = tuple(
        Target(str(item["provider"]), str(item["model"])) for item in entries
    )
    if not selected or effort not in EFFORTS:
        raise ValueError("invalid_model")
    return usage, selected
