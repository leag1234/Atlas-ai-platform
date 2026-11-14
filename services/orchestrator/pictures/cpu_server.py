"""Client of the CPU picture server (infra/pictures-cpu).

The server draws one picture at a time with an open image model and screens
each one with its content classifier; it answers over TLS (its own
certificate, pinned here by file) to a shared token. Configuration:

- ATLAS_PICTURE_CPU_URL: base URL, e.g. https://<address>:8790 (unset: the
  CPU pictures are unavailable and painting by code answers);
- ATLAS_PICTURE_CPU_TOKEN_FILE: file holding the shared token;
- ATLAS_PICTURE_CPU_CA: the server's certificate (PEM), for an https URL;
- ATLAS_PICTURE_CPU_SECONDS: longest wait for the pictures (30 to 200,
  default 100) before painting by code takes over.

The token never reaches a picture job (worker.PASSED) or a log line.
"""

from __future__ import annotations

import json
import os
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

DEFAULT_SECONDS = 100.0
HEALTH_SECONDS = 4.0
# Seconds per picture when the server does not say (COMPUTE3-X16C-32G, 4
# steps: 23.3 s for a new prompt, 19.7 s for the next picture of it).
PICTURE_SECONDS = 22.0
# The client waits this much longer than the wait the server admitted.
GRACE_SECONDS = 15.0
MAX_REPLY_BYTES = 8_000_000


class CpuServerError(RuntimeError):
    """The server could not draw: unreachable, busy, refused or invalid."""

    def __init__(self, code: str, expected: float | None = None) -> None:
        super().__init__(code)
        self.code = code
        # For "busy": the wait the server expected, in seconds.
        self.expected = expected


@dataclass
class Rendered:
    image: str  # base64 PNG
    nsfw: float | None
    render_seconds: float
    queue_seconds: float
    filter_seconds: float


def url() -> str:
    return os.environ.get("ATLAS_PICTURE_CPU_URL", "").strip().rstrip("/")


def configured() -> bool:
    return url().startswith(("https://", "http://"))


def wait_seconds() -> float:
    try:
        value = float(
            os.environ.get("ATLAS_PICTURE_CPU_SECONDS", "") or DEFAULT_SECONDS
        )
    except ValueError:
        return DEFAULT_SECONDS
    return min(max(value, 30.0), 200.0)


def _headers() -> dict[str, str]:
    path = os.environ.get("ATLAS_PICTURE_CPU_TOKEN_FILE", "").strip()
    if not path:
        return {}
    try:
        token = Path(path).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        raise CpuServerError("picture_cpu_token") from None
    return {"Authorization": "Bearer " + token} if token else {}


def _tls() -> ssl.SSLContext | bool:
    if not url().startswith("https://"):
        return False
    authority = os.environ.get("ATLAS_PICTURE_CPU_CA", "").strip()
    try:
        context = ssl.create_default_context(cafile=authority or None)
    except (OSError, ssl.SSLError):
        raise CpuServerError("picture_cpu_certificate") from None
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


async def _call(
    method: str, path: str, body: dict[str, Any] | None, seconds: float
) -> dict[str, Any]:
    if not configured():
        raise CpuServerError("picture_cpu_unconfigured")
    headers = _headers()
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=seconds), trust_env=False
        ) as session:
            async with session.request(
                method,
                url() + path,
                json=body,
                headers=headers,
                ssl=_tls(),
                allow_redirects=False,
            ) as response:
                raw = bytearray()
                async for piece in response.content.iter_chunked(65536):
                    raw.extend(piece)
                    if len(raw) > MAX_REPLY_BYTES:
                        break
                status = response.status
    except TimeoutError:
        raise CpuServerError("picture_cpu_timeout") from None
    except (aiohttp.ClientError, OSError, ssl.SSLError):
        raise CpuServerError("picture_cpu_unreachable") from None
    if len(raw) > MAX_REPLY_BYTES:
        raise CpuServerError("picture_cpu_reply_too_large")
    try:
        reply = json.loads(raw)
    except ValueError:
        raise CpuServerError("picture_cpu_invalid_reply") from None
    if not isinstance(reply, dict):
        raise CpuServerError("picture_cpu_invalid_reply")
    if status == 503:
        expected = reply.get("expected_seconds")
        raise CpuServerError(
            "picture_cpu_" + ("busy" if reply.get("code") == "busy" else "not_ready"),
            float(expected) if isinstance(expected, (int, float)) else None,
        )
    if status != 200:
        raise CpuServerError(f"picture_cpu_http_{status}")
    return reply


async def health(seconds: float = HEALTH_SECONDS) -> dict[str, Any]:
    """{"ready", "queue", "seconds_per_image", "busy_seconds", ...};
    CpuServerError when the server does not answer or is not ready."""
    reply = await _call("GET", "/v1/health", None, seconds)
    if reply.get("ready") is not True:
        raise CpuServerError("picture_cpu_not_ready")
    return reply


def expected_wait(status: dict[str, Any], pictures: int) -> float:
    """Seconds until `pictures` more pictures would be drawn, from the
    server's own measure (busy_seconds, seconds_per_image), else from its
    queue length and PICTURE_SECONDS."""
    per = status.get("seconds_per_image")
    per_image = (
        float(per) if isinstance(per, (int, float)) and per > 0 else PICTURE_SECONDS
    )
    busy = status.get("busy_seconds")
    if isinstance(busy, (int, float)) and busy >= 0:
        ahead = float(busy)
    else:
        queue = status.get("queue")
        ahead = (int(queue) if isinstance(queue, int) else 0) * per_image
    return ahead + pictures * per_image


def _rendered(item: dict[str, Any], queue_seconds: float) -> Rendered:
    image = item.get("image")
    if not isinstance(image, str) or not image:
        raise CpuServerError("picture_cpu_invalid_reply")

    def number(key: str) -> float:
        value = item.get(key)
        return float(value) if isinstance(value, (int, float)) else 0.0

    nsfw = item.get("nsfw")
    return Rendered(
        image=image,
        nsfw=float(nsfw) if isinstance(nsfw, (int, float)) else None,
        render_seconds=number("render_seconds"),
        queue_seconds=queue_seconds,
        filter_seconds=number("filter_seconds"),
    )


async def render_set(
    prompt: str, seeds: list[int], max_wait: float
) -> list[Rendered | None]:
    """The pictures of `prompt`, one per seed, drawn one after the other as
    one job. The server admits the job only if it expects to finish within
    `max_wait` seconds (else CpuServerError "picture_cpu_busy" at once); a
    picture it could not draw is None.

    A server still on the one-picture form (it answers 400 to a job) is
    asked for each picture in turn, within the same wait."""
    started = time.monotonic()
    try:
        reply = await _call(
            "POST",
            "/v1/render",
            {"prompt": prompt, "seeds": seeds, "max_wait_seconds": round(max_wait, 1)},
            max_wait + GRACE_SECONDS,
        )
    except CpuServerError as error:
        if error.code != "picture_cpu_http_400":
            raise
        return await _one_by_one(prompt, seeds, started + max_wait + GRACE_SECONDS)
    items = reply.get("images")
    if not isinstance(items, list):
        raise CpuServerError("picture_cpu_invalid_reply")
    queue = reply.get("queue_seconds")
    waited = float(queue) if isinstance(queue, (int, float)) else 0.0
    by_seed = {item.get("seed"): item for item in items if isinstance(item, dict)}
    return [
        _rendered(by_seed[seed], waited) if seed in by_seed else None for seed in seeds
    ]


async def _one_by_one(
    prompt: str, seeds: list[int], end: float
) -> list[Rendered | None]:
    """The older server form: one request per picture."""
    out: list[Rendered | None] = []
    for seed in seeds:
        left = end - time.monotonic()
        if left <= 1:
            out.append(None)
            continue
        reply = await _call(
            "POST", "/v1/render", {"prompt": prompt, "seed": seed}, left
        )
        queue = reply.get("queue_seconds")
        out.append(
            _rendered(reply, float(queue) if isinstance(queue, (int, float)) else 0.0)
        )
    return out
