"""M8 task policy: validated capabilities and conservative two-attempt reservation."""

from packages.profiles import (
    HIDDEN_PROFILES,
    PICTURE_ROLES,
    PROFILES,
    enabled_hidden_profiles,
)
from packages.limits import ProviderLimitError
from packages.context_limit import ContextExceeded

from dataclasses import dataclass
from decimal import Decimal
import json
import os
from pathlib import Path
import re
from typing import Literal

import yaml

from provider_settings import (
    REASONING_EFFORTS,
    api_key_for,
    configure,
    presets,
    reasoning_control,
    selected,
)


# Extra output room for the first attempt of a model with configured reasoning.
# The orchestrator reserves and bounds the same room (/agent/config).
REASONING_OUTPUT_TOKENS = {"low": 2000, "medium": 6000, "high": 12000}

Task = Literal["text", "code", "vision"]


def classify_task(messages: list[dict[str, object]]) -> Task:
    texts = []
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            if any(
                isinstance(p, dict) and p.get("type") == "image_url" for p in content
            ):
                return "vision"
            content = " ".join(
                str(p.get("text", "")) for p in content if isinstance(p, dict)
            )
        if not isinstance(content, str) or not content.strip():
            raise ValueError("invalid_input")
        texts.append(content)
    if not texts:
        raise ValueError("invalid_input")
    combined = "\n".join(texts)
    # Software performance analysis needs instruction-level code expertise;
    # bare hardware specifications stay on the text route.
    if re.search(r"(?i)\b(assembly|assembler|assembleur)\b", combined) or all(
        re.search(pattern, combined, re.I)
        for pattern in (
            r"\b(cpu|processor|processeur)\b",
            r"\b(software|logiciel|instructions?|program\w*)\b",
            r"\b(throughput|d[eé]bit|cycles?|performance|speed)\b",
        )
    ):
        return "code"
    if re.search(
        r"(?i)\b(python|javascript|typescript|sql|rust|java|bash|debug\w*|refactor\w*|programm\w*|coding)\b|c\+\+|c#|```|\b(write|écris|écrire|schreib\w*|implement\w*|scrivi|escrib\w*)\b.{0,80}\b(code|function|fonction|funktion|función|funzione)\b",
        "\n".join(texts),
    ):
        return "code"
    return "text"


@dataclass(frozen=True)
class Plan:
    task_type: Task
    primary: str
    fallback: str
    primary_bound: Decimal
    fallback_bound: Decimal
    incoming: int

    @property
    def reserved_eur(self) -> Decimal:
        return self.primary_bound + self.fallback_bound


class ServerlessPolicy:
    def __init__(self, *, allow_unpriced: bool = False) -> None:
        root = Path(__file__).parent
        routing = yaml.safe_load((root / "routing.yaml").read_text())
        self.providers: dict[str, str] = {}
        self.config = routing["serverless"]
        self.public_profiles: dict[str, str] = routing["public_profiles"]
        hidden_roles: dict[str, dict[str, object]] = routing.get("hidden_roles", {})
        declared_hidden_profiles: dict[str, str] = routing.get("hidden_profiles", {})
        if set(declared_hidden_profiles) != set(HIDDEN_PROFILES):
            raise ValueError("invalid_hidden_profiles")
        if any(
            role not in hidden_roles or role in self.config
            for role in declared_hidden_profiles.values()
        ):
            raise ValueError("invalid_hidden_roles")
        for entry in hidden_roles.values():
            provider = entry.get("provider")
            preset = presets().get(str(provider), {}) if provider is not None else {}
            if (
                not provider
                or not preset.get("base_url")
                or not preset.get("api_key_env")
                or entry.get("fallback") not in self.config
            ):
                raise ValueError("invalid_hidden_roles")
        if set(self.public_profiles) != set(PROFILES) or any(
            role not in self.config for role in self.public_profiles.values()
        ):
            raise ValueError("invalid_public_profiles")
        # Roles of the picture chain name a declared profile, public or hidden.
        self.picture_roles: dict[str, str] = routing.get("picture_roles", {})
        if set(self.picture_roles) != set(PICTURE_ROLES) or any(
            profile not in self.public_profiles
            and profile not in declared_hidden_profiles
            for profile in self.picture_roles.values()
        ):
            raise ValueError("invalid_picture_roles")
        self.prices = yaml.safe_load((root / "pricing.yaml").read_text())["models"]
        self.config, self.prices = configure(self.config, self.prices)
        self.hidden_profiles: dict[str, str] = {}
        for profile, role in declared_hidden_profiles.items():
            if profile not in enabled_hidden_profiles():
                continue
            entry = hidden_roles[role]
            provider = str(entry["provider"])
            if not api_key_for(provider):
                raise ValueError("provider_key_missing")
            self.config[role] = entry
            self.hidden_profiles[profile] = role
            self.providers[str(entry["model"])] = provider
        self.models: dict[str, str] = {}
        for role, entry in self.config.items():
            model = os.environ.get(entry["env"], entry["model"])
            if os.environ.get("ATLAS_PROVIDER") and not model:
                model = entry["model"]
            if model not in self.prices:
                if not allow_unpriced:
                    raise ValueError("missing_price")
                self.prices[model] = {
                    "input_eur_per_mtok": "0",
                    "output_eur_per_mtok": "0",
                    "unpriced": True,
                }
            if model not in entry["capabilities"]:
                raise ValueError("missing_capabilities")
            for key in ("input_eur_per_mtok", "output_eur_per_mtok"):
                value = Decimal(str(self.prices[model][key]))
                if not value.is_finite() or value < 0:
                    raise ValueError("invalid_price")
            self.models[role] = model
            if provider := entry.get("provider"):
                existing = self.providers.get(model)
                if existing is not None and existing != provider:
                    raise ValueError("model_provider_conflict")
                self.providers[model] = str(provider)
            elif model in self.providers:
                raise ValueError("model_provider_conflict")
        self.efforts = self.reasoning_efforts()

    @property
    def profile_roles(self) -> dict[str, str]:
        return self.public_profiles | self.hidden_profiles

    def reasoning_efforts(self) -> dict[str, str]:
        """Configured reasoning effort per model; none unless an operator sets one.

        Sources, last wins: the model's routing capabilities (`reasoning_effort`),
        the selected provider preset (`reasoning_efforts`), then the JSON object
        in ATLAS_REASONING_EFFORTS ({"<model id>": "none|low|medium|high"}).
        """
        preset = (selected() or {}).get("reasoning_efforts") or {}
        raw = os.environ.get("ATLAS_REASONING_EFFORTS", "").strip()
        try:
            overrides = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise ValueError("invalid_reasoning_efforts") from None
        if not isinstance(overrides, dict) or not isinstance(preset, dict):
            raise ValueError("invalid_reasoning_efforts")
        if set(overrides) - set(self.models.values()):
            raise ValueError("unknown_reasoning_model")
        efforts: dict[str, str] = {}
        for role, entry in self.config.items():
            model = self.models[role]
            effort = overrides.get(
                model,
                preset.get(
                    model,
                    entry["capabilities"][model].get("reasoning_effort", "none"),
                ),
            )
            if effort not in REASONING_EFFORTS:
                raise ValueError("invalid_reasoning_effort")
            if (
                effort != "none"
                and reasoning_control(getattr(self, "providers", {}).get(model), model)
                is None
            ):
                raise ValueError("reasoning_unsupported")
            efforts[model] = max(
                efforts.get(model, "none"), effort, key=REASONING_EFFORTS.index
            )
        return efforts

    def cost(self, model: str, incoming: int, outgoing: int) -> Decimal:
        price = self.prices[model]
        return (
            incoming * Decimal(str(price["input_eur_per_mtok"]))
            + outgoing * Decimal(str(price["output_eur_per_mtok"]))
        ) / 1_000_000

    def configuration(self) -> dict[str, object]:
        # Covers every primary/fallback pair, including an unknown primary failure.
        configuration: dict[str, object] = {
            key: str(
                max(
                    Decimal(str(self.prices[self.models[role]][key]))
                    + Decimal(str(self.prices[self.models[entry["fallback"]]][key]))
                    for role, entry in self.config.items()
                    if "fallback" in entry
                )
            )
            for key in ("input_eur_per_mtok", "output_eur_per_mtok")
        } | {"max_tokens": 2048, "gateway_reserves_quality": True}
        if room := self.reasoning_room():
            configuration["reasoning_output_tokens"] = room
        if profile_room := self.profile_reasoning_room():
            configuration["profile_reasoning_output_tokens"] = profile_room
        return configuration

    def reasoning_room(self) -> int:
        """Largest first-attempt widening any configured reasoning may receive."""
        return max(
            (
                REASONING_OUTPUT_TOKENS[effort]
                for role, entry in self.config.items()
                if role not in self.hidden_profiles.values()
                for effort in (self.efforts.get(self.models[role], "none"),)
                if effort != "none"
            ),
            default=0,
        )

    def profile_reasoning_room(self) -> dict[str, int]:
        return {
            profile: REASONING_OUTPUT_TOKENS[self.efforts[self.models[role]]]
            for profile, role in self.hidden_profiles.items()
            if self.efforts.get(self.models[role], "none") != "none"
        }

    def reserve(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> Plan:
        task = classify_task(messages)
        primary = self.models[task]
        fallback_role = self.config[task]["fallback"]
        fallback = self.models[fallback_role]
        incoming = (
            len(
                json.dumps(
                    {"messages": messages, "tools": tools}, ensure_ascii=False
                ).encode()
            )
            + 256
        )
        # Image pixel tokens can exceed compressed bytes; reserve worst supported image size.
        images = 0
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                images += sum(
                    isinstance(p, dict) and p.get("type") == "image_url"
                    for p in content
                )
        incoming += images * 16384
        for role, model in ((task, primary), (fallback_role, fallback)):
            capabilities = self.config[role]["capabilities"][model]
            if task == "vision" and not capabilities.get("vision"):
                raise ValueError("missing_capabilities")
            if incoming + 2048 > self.config[role]["capabilities"][model]["context"]:
                raise ContextExceeded(
                    incoming + 2048, self.config[role]["capabilities"][model]["context"]
                )
        plan = Plan(
            task,
            primary,
            fallback,
            self.cost(primary, incoming, 2048),
            self.cost(fallback, incoming, 2048),
            incoming,
        )
        if plan.reserved_eur > Decimal("0.05"):
            raise ProviderLimitError(
                "cost_budget", int(plan.reserved_eur * 1000000), 50000, "microEUR"
            )
        return plan
