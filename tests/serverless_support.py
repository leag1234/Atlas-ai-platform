"""Test configuration follows gateway aliases without copying model identifiers."""

from pathlib import Path
import yaml


def environment() -> dict[str, str]:
    roles = yaml.safe_load(Path("services/model-gateway/routing.yaml").read_text())[
        "serverless"
    ]
    return {entry["env"]: entry["model"] for entry in roles.values()} | {
        "SCW_GENERATIVE_BASE_URL": "https://example.invalid/v1",
        "SCW_GENERATIVE_API_KEY": "test-only",
    }
