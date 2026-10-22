"""Published public model selector contract, independent of provider identifiers."""

import json
from pathlib import Path


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
    role = "vision" if vision else routing["public_profiles"][profile]
    entry = routing["serverless"][role]
    model = os.environ.get(entry["env"], entry["model"])
    window = entry["capabilities"][model]["context"]
    if type(window) is not int or window <= 0:
        raise ValueError("invalid_context_window")
    return window
