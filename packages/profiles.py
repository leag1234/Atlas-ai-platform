"""Published public model selector contract, independent of provider identifiers."""

import json
from pathlib import Path


# Open WebUI serves a model avatar only from an http(s) or data:image URL and
# falls back to its favicon otherwise, so the avatar is embedded.
MODEL_PROFILE_IMAGE_URL = (
    "data:image/svg+xml;base64,"
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAx"
    "MjggMTI4IiByb2xlPSJpbWciIGFyaWEtbGFiZWw9IkF0bGFzIj4KICA8dGl0bGU+QXRsYXM8"
    "L3RpdGxlPgogIDxyZWN0IHdpZHRoPSIxMjgiIGhlaWdodD0iMTI4IiByeD0iMjgiIGZpbGw9"
    "IiMxQTFBMTgiLz4KICA8ZyB0cmFuc2Zvcm09InRyYW5zbGF0ZSgxMyAxMykgc2NhbGUoMS4w"
    "MikiPgogICAgPHBhdGggZD0iTTIyLjYgNTAgQTM0IDI0IDAgMSAxIDY1LjYgNjAiIGZpbGw9"
    "Im5vbmUiIHN0cm9rZT0iI0VERUNFOCIgc3Ryb2tlLXdpZHRoPSIxMCIgc3Ryb2tlLWxpbmVj"
    "YXA9InJvdW5kIi8+CiAgICA8Y2lyY2xlIGN4PSIzMyIgY3k9Ijc2IiByPSIxMyIgZmlsbD0i"
    "I0UwNUEyQiIvPgogIDwvZz4KPC9zdmc+Cg=="
)


def load_profiles() -> tuple[str, ...]:
    contract = Path(__file__).resolve().parents[1] / "contracts/m21-profiles.json"
    models = json.loads(contract.read_text())["models"]
    if (
        not isinstance(models, list)
        or not models
        or any(not isinstance(name, str) or not name for name in models)
        or len(set(models)) != len(models)
    ):
        raise ValueError("invalid_public_profiles")
    return tuple(models)


PROFILES = load_profiles()


def context_window(profile: str, *, vision: bool = False) -> int:
    """Resolve the active gateway model's declared capacity, including overrides."""
    import os
    import yaml

    path = Path(__file__).resolve().parents[1] / "services/model-gateway/routing.yaml"
    routing = yaml.safe_load(path.read_text())
    if provider := os.environ.get("ATLAS_PROVIDER"):
        preset = yaml.safe_load(path.with_name("providers.yaml").read_text())[provider]
        declared = preset["context"]
        if isinstance(declared, dict):
            declared = declared["vision" if vision else "text"]
        window = int(os.environ.get("ATLAS_MODEL_CONTEXT") or declared)
        if window <= 0:
            raise ValueError("invalid_context_window")
        return window
    role = "vision" if vision else routing["public_profiles"][profile]
    entry = routing["serverless"][role]
    model = os.environ.get(entry["env"], entry["model"])
    window = entry["capabilities"][model]["context"]
    if type(window) is not int or window <= 0:
        raise ValueError("invalid_context_window")
    return window
