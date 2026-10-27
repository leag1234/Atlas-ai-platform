"""Published public model selector contract, independent of provider identifiers."""

import json
from pathlib import Path


# Open WebUI 0.11.3 accepts a model avatar only as an http(s) URL or a raster
# data URL (png, jpeg, gif, webp; SVG is rejected) and otherwise shows its
# favicon, so the PNG of assets/brand/svg/atlas-model-avatar.svg is embedded.
MODEL_PROFILE_IMAGE_URL = (
    "data:image/png;base64,"
    "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"
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
