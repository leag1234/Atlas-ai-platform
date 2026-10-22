"""M8 task policy: validated capabilities and conservative two-attempt reservation."""

from packages.profiles import PROFILES
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
    def __init__(self) -> None:
        root = Path(__file__).parent
        routing = yaml.safe_load((root / "routing.yaml").read_text())
        self.config = routing["serverless"]
        self.public_profiles: dict[str, str] = routing["public_profiles"]
        if set(self.public_profiles) != set(PROFILES) or any(
            role not in self.config for role in self.public_profiles.values()
        ):
            raise ValueError("invalid_public_profiles")
        self.prices = yaml.safe_load((root / "pricing.yaml").read_text())["models"]
        self.models: dict[str, str] = {}
        for role, entry in self.config.items():
            model = os.environ.get(entry["env"], entry["model"])
            if model not in self.prices:
                raise ValueError("missing_price")
            if model not in entry["capabilities"]:
                raise ValueError("missing_capabilities")
            for key in ("input_eur_per_mtok", "output_eur_per_mtok"):
                value = Decimal(str(self.prices[model][key]))
                if not value.is_finite() or value < 0:
                    raise ValueError("invalid_price")
            self.models[role] = model

    def cost(self, model: str, incoming: int, outgoing: int) -> Decimal:
        price = self.prices[model]
        return (
            incoming * Decimal(str(price["input_eur_per_mtok"]))
            + outgoing * Decimal(str(price["output_eur_per_mtok"]))
        ) / 1_000_000

    def configuration(self) -> dict[str, object]:
        # Covers every primary/fallback pair, including an unknown primary failure.
        return {
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
