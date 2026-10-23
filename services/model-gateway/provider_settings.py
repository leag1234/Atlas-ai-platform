"""Opt-in provider presets; legacy Scaleway settings retain their existing meaning."""

import json
import logging
import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


@lru_cache(maxsize=1)
def presets() -> dict[str, Any]:
    text = Path(__file__).with_name("providers.yaml").read_text()
    value: dict[str, Any] = json.loads(
        "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
    )
    return value


def selected() -> dict[str, Any] | None:
    name = os.environ.get("ATLAS_PROVIDER")
    if not name:
        return None
    if name not in presets():
        raise ValueError("unknown_provider")
    return dict(presets()[name])


def validate_endpoint(value: str, local: bool = False) -> str:
    url = urlsplit(value)
    if (
        not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or (url.scheme != "https" and not (local and url.scheme == "http"))
    ):
        raise ValueError("provider_url_requires_https_without_credentials")
    return value.rstrip("/")


def endpoint() -> str:
    preset = selected()
    value = (
        os.environ.get("ATLAS_PROVIDER_BASE_URL") or preset.get("base_url")
        if preset is not None
        else os.environ["SCW_GENERATIVE_BASE_URL"]
    )
    return validate_endpoint(
        str(value or ""), os.environ.get("ATLAS_PROVIDER") == "local"
    )


def api_key() -> str:
    if selected() is None:
        return os.environ["SCW_GENERATIVE_API_KEY"]
    return os.environ.get("ATLAS_PROVIDER_API_KEY", "")


def request_options(
    body: dict[str, Any], provider: str | None = None
) -> dict[str, Any]:
    name = os.environ.get("ATLAS_PROVIDER", "") if provider is None else provider
    body = dict(body)
    if name == "hetzner" and isinstance(messages := body.get("messages"), list):
        # Hetzner's chat template accepts system instructions only at the start.
        # Quality finalization/recovery adds instructions after tool evidence.
        # Merge only system messages; never promote untrusted history or mutate it.
        systems = [message for message in messages if message.get("role") == "system"]
        if systems:
            contents = [message["content"] for message in systems]
            body["messages"] = [
                {
                    "role": "system",
                    "content": "\n\n".join(contents)
                    if all(isinstance(content, str) for content in contents)
                    else [
                        part
                        for content in contents
                        for part in (
                            content
                            if isinstance(content, list)
                            else [{"type": "text", "text": content}]
                        )
                    ],
                },
                *[message for message in messages if message.get("role") != "system"],
            ]
    if name not in ("", "scaleway"):
        body.pop("reasoning_effort", None)
        if not body.get("tools"):
            body.pop("tools", None)
    if name:
        body.update(presets()[name].get("request_options", {}))
    return body


@lru_cache(maxsize=32)
def warn_unpriced(provider: str, model: str) -> None:
    logging.getLogger(__name__).warning(
        "Provider %s model %s: verified EUR price unavailable; monetary budget "
        "ceiling disabled, reported cost is unpriced (not free). Provider billing "
        "still applies. Token, tool and time limits remain enabled.",
        provider,
        model,
    )


def configure(
    config: dict[str, Any], prices: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    preset = selected()
    if preset is None:
        return config, prices
    text = os.environ.get("ATLAS_TEXT_MODEL") or preset.get("text_model")
    vision = os.environ.get("ATLAS_VISION_MODEL") or preset.get("vision_model")
    if not text:
        raise ValueError("provider_text_model_required")
    # A default Scaleway preset keeps distinct code, fast and vision fallback roles.
    if os.environ["ATLAS_PROVIDER"] == "scaleway" and not os.environ.get(
        "ATLAS_TEXT_MODEL"
    ):
        return config, prices
    roles: dict[str, Any] = {}
    rates: dict[str, Any] = {}
    for role in config:
        model = vision if role in ("vision", "visionprevious") and vision else text
        context = preset["context"]
        if isinstance(context, dict):
            context = context[
                "vision" if role in ("vision", "visionprevious") and vision else "text"
            ]
        price = (
            preset.get("vision_price", preset.get("price"))
            if model == vision
            else preset.get("price")
        )
        if model not in (preset.get("text_model"), preset.get("vision_model")):
            price = None
        factor = Decimal(1)
        if price and price["currency"] != "EUR":
            conversion = os.environ.get("ATLAS_USD_TO_EUR")
            if conversion:
                factor = Decimal(conversion)
                if not factor.is_finite() or factor <= 0:
                    raise ValueError("invalid_currency_conversion")
            else:
                price = None
        if price is None:
            warn_unpriced(os.environ["ATLAS_PROVIDER"], model)
        rates[model] = {
            "input_eur_per_mtok": str(Decimal(str(price["input"])) * factor)
            if price
            else "0",
            "output_eur_per_mtok": str(Decimal(str(price["output"])) * factor)
            if price
            else "0",
            "unpriced": price is None,
        }
        roles[role] = {
            "env": "ATLAS_VISION_MODEL"
            if role in ("vision", "visionprevious")
            else "ATLAS_TEXT_MODEL",
            "model": model,
            "fallback": "vision"
            if role == "visionprevious"
            else "visionprevious"
            if role == "vision"
            else "text",
            "capabilities": {
                model: {
                    "context": int(os.environ.get("ATLAS_MODEL_CONTEXT", context)),
                    "tools": True,
                    "vision": bool(vision and model == vision),
                }
            },
        }
    return roles, rates
