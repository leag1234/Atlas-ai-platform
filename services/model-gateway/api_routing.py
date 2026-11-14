"""Versioned public API usage routing, kept separate from internal raw routes."""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from packages import privacy


PUBLIC_MODELS = (
    "atlas-code",
    "atlas-chat",
    "atlas-dev",
    "atlas-code-2026-10-30",
    "atlas-chat-2026-10-30",
)
EFFORTS = ("none", "low", "medium", "high")


@dataclass(frozen=True)
class Target:
    provider: str
    model: str
    # Provider-specific value sent for the requested level (None: forward as is).
    effort: str | None = None
    context: int = 0
    privacy: str = "shared"


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
    if pinned_target(name) is not None:
        return name
    return public_model(name) if name in PUBLIC_MODELS else "atlas-code"


def pinned_target(name: str) -> Target | None:
    """The single routing target a precise model id pins, or None for aliases."""
    provider, separator, model = name.partition("/")
    if not separator or not provider or not model:
        return None
    for usage in ("atlas-code", "atlas-chat"):
        for effort in EFFORTS:
            entries = config().get("usages", {}).get(usage, {}).get(effort)
            if not isinstance(entries, list):
                continue
            for item in entries:
                if (
                    isinstance(item, dict)
                    and item.get("provider") == provider
                    and item.get("model") == model
                ):
                    return Target(
                        provider,
                        model,
                        str(item["effort"]) if item.get("effort") else None,
                        int(item["context"]),
                        "owned" if item.get("privacy") == "owned" else "shared",
                    )
    return None


def pinned_models() -> list[str]:
    """Precise model ids, one per routing target, in configuration order."""
    ids: list[str] = []
    for usage in ("atlas-code", "atlas-chat"):
        for effort in EFFORTS:
            entries = config().get("usages", {}).get(usage, {}).get(effort)
            if not isinstance(entries, list):
                continue
            for item in entries:
                if not isinstance(item, dict):
                    continue
                identifier = f"{item['provider']}/{item['model']}"
                if identifier not in ids:
                    ids.append(identifier)
    return ids


def targets(
    name: str, effort: str, *, strict: bool = False, pinned: bool = False
) -> tuple[str, tuple[Target, ...]]:
    usage = public_model(name)
    strict = strict or privacy.owned_only()
    if pinned:
        target = pinned_target(name)
        if target is None:
            raise ValueError("invalid_model")
        if strict and target.privacy != "owned":
            raise privacy.refusal("inference")
        return usage, (target,)
    entries = config().get("usages", {}).get(usage, {}).get(effort)
    if usage not in ("atlas-code", "atlas-chat") or not isinstance(entries, list):
        raise ValueError("invalid_model")
    selected = tuple(
        Target(
            str(item["provider"]),
            str(item["model"]),
            str(item["effort"]) if item.get("effort") else None,
            int(item["context"]),
            "owned" if item.get("privacy") == "owned" else "shared",
        )
        for item in entries
    )
    if strict:
        selected = tuple(target for target in selected if target.privacy == "owned")
        if not selected:
            raise privacy.refusal("inference")
    if not selected or effort not in EFFORTS:
        raise ValueError("invalid_model")
    return usage, selected


def supports_structured_output(name: str, effort: str) -> bool:
    """Whether every target of a public route guarantees strict json_schema.

    A route guarantees the schema only when each of its targets runs on a
    provider whose preset declares native structured output; Atlas then
    validates the final JSON against the schema with one retry. Otherwise the
    request must be refused, never answered with free text.
    """
    from provider_settings import presets

    try:
        _, entries = targets(name, effort, pinned=pinned_target(name) is not None)
    except privacy.StrictPrivacyUnavailable:
        raise
    except ValueError:
        return False
    declared = config().get("structured_output", {})
    return bool(entries) and all(
        bool(declared.get(target.provider))
        and presets().get(target.provider, {}).get("json_response_format") is True
        for target in entries
    )
