"""Resolve worktree-local listener ports and Docker resource names."""

from dataclasses import dataclass
import os


DEFAULT_PORTS = {
    "chat_ui": 3000,
    "terminal": 8000,
    "gateway": 8010,
    "retrieval": 8011,
    "adapter": 8020,
    "tts": 8021,
    "stt": 8022,
    "postgres": 5432,
    "searxng": 8888,
    "runner": 13712,
}


def port_base(value: str | None = None) -> int:
    """Return the configured offset, rejecting values that cannot form TCP ports."""
    raw = os.environ.get("ATLAS_PORT_BASE", "0") if value is None else value
    try:
        base = int(raw)
    except ValueError as exc:
        raise ValueError("invalid_atlas_port_base") from exc
    if base < 0 or max(DEFAULT_PORTS.values()) + base > 65535:
        raise ValueError("invalid_atlas_port_base")
    return base


@dataclass(frozen=True)
class AtlasPorts:
    base: int

    def port(self, service: str) -> int:
        try:
            return DEFAULT_PORTS[service] + self.base
        except KeyError as exc:
            raise ValueError("unknown_atlas_port:" + service) from exc

    def url(self, service: str, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port(service)}"

    def name(self, default: str) -> str:
        return default if self.base == 0 else f"{default}-{self.base}"


def atlas_ports() -> AtlasPorts:
    return AtlasPorts(port_base())
