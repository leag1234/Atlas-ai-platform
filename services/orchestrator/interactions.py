"""Private, append-only interaction journal; never persist injected credentials."""

import fcntl
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


@dataclass
class Interaction:
    steps: list[dict[str, object]] = field(default_factory=list)
    verification: dict[str, object] | None = None
    location_used: str = ""
    nearby_source: str = ""
    context_manifest: dict[str, object] = field(default_factory=lambda: {"sources": []})
    context_samples: list[dict[str, object]] = field(default_factory=list)
    provider_retries: int = 0
    commons_requests: list[dict[str, object]] = field(default_factory=list)
    retrieved_urls: list[str] = field(default_factory=list)
    tool_urls: list[str] = field(default_factory=list)
    terminal_commands: int = 0
    terminal_failed_commands: int = 0
    files: list[str] = field(default_factory=list)
    file_strategies: list[str] = field(default_factory=list)
    documents: list[dict[str, str | int | bool]] = field(default_factory=list)
    uploaded_images: list[str] = field(default_factory=list)
    provider_model: str = ""
    reasoning_effort: str = "none"
    reasoning: str = ""
    trace_tokens: int = 0
    answer_tokens: int = 0
    token_split_estimated: bool = False
    reasoning_retried: bool = False
    uploads: list[dict[str, int]] = field(default_factory=list)
    rejection: dict[str, str] = field(default_factory=dict)
    startup_seconds: float = 0.0
    execution: str = "live"
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    request_id: str = field(default_factory=lambda: "chatcmpl-" + uuid4().hex)
    question: str = ""
    reponse: str = ""
    modele_utilise: str | None = None
    route_decision: str | None = None
    latence_ms: dict[str, float] = field(
        default_factory=lambda: {"retrieval": 0.0, "generation": 0.0, "total": 0.0}
    )
    chunks_recuperes: list[dict[str, object]] = field(default_factory=list)
    citations: list[dict[str, object]] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=lambda: {"in": 0, "out": 0})
    cout_eur: float = 0.0
    erreurs: list[str] = field(default_factory=list)
    state: str = "received"
    task_type: str = "text"
    images: list[dict[str, int | str]] = field(default_factory=list)


def write_interaction(item: Interaction, directory: Path) -> None:
    secrets = [
        v
        for k, v in os.environ.items()
        if len(v) >= 8 and re.search(r"KEY|TOKEN|SECRET|PASSWORD|DSN", k)
    ]

    def scrub(value: object) -> object:
        if isinstance(value, str):
            value = re.sub(r"data:image/[^,\s]+,[A-Za-z0-9+/=]+", "[IMAGE]", value)
            for secret in secrets:
                value = value.replace(secret, "[REDACTED]")
            return re.sub(
                r"(?i)(?:Bearer\s+\S+|sk-[\w-]{12,}|(?:api[_-]?key|password|secret|token)\s*[=:]\s*[^\s,;]+)",
                "[REDACTED]",
                value,
            )
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value

    if directory.is_symlink():
        raise OSError("unsafe_log_directory")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / (datetime.now(timezone.utc).date().isoformat() + ".jsonl")
    record = asdict(item)
    if item.rejection.get("code", "").startswith("image_limit_"):
        # Count the applied rule without retaining the refused image request.
        record["question"] = ""
    body = json.dumps(scrub(record), ensure_ascii=True, allow_nan=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        os.fchmod(output.fileno(), 0o600)
        fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        output.write(body)
        output.flush()
