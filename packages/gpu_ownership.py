"""Shared GPU identity and explicit development opt-in."""

import hashlib
import os
from pathlib import Path
import re


def gpu_owner() -> str:
    owner = os.environ.get("ATLAS_GPU_OWNER")
    if owner is None:
        owner = (
            "demo"
            if os.environ.get("ATLAS_STABLE") == "1"
            else "dev-"
            + hashlib.sha256(str(Path.cwd().resolve()).encode()).hexdigest()[:10]
        )
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", owner):
        raise ValueError(
            "Invalid ATLAS_GPU_OWNER: use 1-48 lowercase letters, digits or hyphens"
        )
    return owner


def gpu_name() -> str:
    owner = gpu_owner()
    return "atlas-gpu" if owner == "demo" else "atlas-gpu-" + owner


def gpu_allowed() -> bool:
    return (
        os.environ.get("ATLAS_STABLE") == "1" or os.environ.get("ATLAS_DEV_GPU") == "1"
    )


def image_worker_address() -> str:
    """Configured workers are usable without permission to manage infrastructure."""
    if address := os.environ.get("ATLAS_IMAGE_GPU_IP"):
        return address
    try:
        return Path("BRAIN/gpu_ip.txt").read_text().strip()
    except OSError:
        return ""


class GPUDisabled(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Image generation is not available on this instance.")


def require_gpu() -> None:
    if not gpu_allowed():
        raise GPUDisabled()
