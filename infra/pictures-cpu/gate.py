"""Front of the CPU picture server: one queue, TLS, a shared token, the filter.

Runs inside the container built by infra/pictures-cpu/Dockerfile. It starts
the image engine (sd-server, listening on the container's loopback only),
loads the content classifier once, and serves two authenticated endpoints:

  GET  /v1/health  -> {"ready", "queue" (pictures still to draw), "jobs",
                        "seconds_per_image", "busy_seconds", "model"}
  POST /v1/render  {"prompt", "seeds": [1 to 4 seeds], "max_wait_seconds"}
                   -> {"images": [{"seed", "image" (base64 PNG, 768 px),
                       "nsfw" (0..1), "render_seconds", "filter_seconds"}],
                       "queue_seconds", "expected_seconds", "model"}
                   (the older {"prompt", "seed"} form answers one picture)

One picture is drawn at a time (the engine uses every core). Requests are
jobs served first come, first served; the pictures of one job are drawn one
after the other (the engine keeps the prompt's encoding: the second picture
is about 3.5 s faster). A job is admitted only when the wait it would have
(the pictures queued before it, the time per picture measured on this
server, its own pictures) fits its max_wait_seconds; otherwise it gets 503
{"code": "busy", "expected_seconds"} at once, so that the caller falls back
without waiting. A job whose caller has given up (past its wait and a grace)
is dropped instead of drawn. Sizes, steps and guidance are fixed here, never
taken from the request. The engine and the classifier run offline: the
container has no outbound network (install.sh).
"""

from __future__ import annotations

import base64
import hmac
import io
import json
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

ENGINE = "http://127.0.0.1:7860"
PORT = int(os.environ.get("GATE_PORT", "8790"))
SECRET_DIR = os.environ.get("GATE_SECRET_DIR", "/run/pictures-secret")
WEIGHTS = os.environ.get("GATE_WEIGHTS", "/w")
MODEL = os.environ.get("GATE_MODEL", "klein")
THREADS = int(os.environ.get("GATE_THREADS", str(os.cpu_count() or 4)))
# Pictures (not requests) that may be queued, the one being drawn included.
MAX_QUEUE = int(os.environ.get("GATE_MAX_QUEUE", "8"))
MAX_SEEDS = 4
# Seconds per picture before any is measured (COMPUTE3-X16C-32G, 4 steps:
# 23.3 s for a new prompt, 19.7 s for the next picture of the same prompt).
FIRST_ESTIMATE = 22.0
# A caller gives up at its wait plus this; a job still queued then is dropped.
GRACE_SECONDS = 20.0
MAX_PROMPT_CHARS = 1500
MAX_BODY_BYTES = 16_384
DRAW_PX = 512
SHOW_PX = 768
ENGINE_SECONDS = 600
# Measured settings (spike round 25): 4 steps for the 4-bit model, 8 for the
# fallback model, guidance 1, 512 px drawn then a Lanczos enlargement to 768.
MODELS: dict[str, dict[str, Any]] = {
    "klein": {
        "diffusion": "klein-Q4_K.gguf",
        "llm": "qwen3-4b-Q8_0.gguf",
        "vae": "klein-vae.safetensors",
        "steps": 4,
    },
    "zimage": {
        "diffusion": "zimage-Q4_K.gguf",
        "llm": "qwen3-4b-instruct2507-Q8_0.gguf",
        "vae": "zimage-vae.safetensors",
        "steps": 8,
    },
}


@dataclass(eq=False)
class Job:
    count: int
    deadline: float | None
    done: int = 0


class Queue:
    """First come, first served jobs, admitted only when they fit their wait."""

    def __init__(
        self,
        per_image: float = FIRST_ESTIMATE,
        max_images: int = MAX_QUEUE,
        clock: Any = time.monotonic,
    ) -> None:
        self.condition = threading.Condition()
        self.jobs: deque[Job] = deque()
        self.per_image = per_image
        self.max_images = max_images
        self.clock = clock
        self.drawing_since: float | None = None

    def images(self) -> int:
        return sum(job.count - job.done for job in self.jobs)

    def busy_seconds(self) -> float:
        """Expected time until every queued picture is drawn."""
        total = self.images() * self.per_image
        if self.drawing_since is not None:
            total -= min(self.per_image, self.clock() - self.drawing_since)
        return max(0.0, total)

    def admit(self, count: int, max_wait: float | None) -> tuple[Job | None, float]:
        """(the job, or None when it would not fit, and its expected wait)."""
        with self.condition:
            expected = self.busy_seconds() + count * self.per_image
            if self.images() + count > self.max_images or (
                max_wait is not None and expected > max_wait
            ):
                return None, expected
            deadline = (
                None if max_wait is None else self.clock() + max_wait + GRACE_SECONDS
            )
            job = Job(count, deadline)
            self.jobs.append(job)
            return job, expected

    def expired(self, job: Job) -> bool:
        return job.deadline is not None and self.clock() > job.deadline

    def wait_turn(self, job: Job) -> bool:
        """Block until the job is first; False when its caller has given up."""
        with self.condition:
            while job in self.jobs and self.jobs[0] is not job:
                self.condition.wait(timeout=1.0)
            if job not in self.jobs:
                return False
            if self.expired(job):
                self._remove(job)
                return False
            return True

    def start_image(self) -> None:
        with self.condition:
            self.drawing_since = self.clock()

    def image_done(self, job: Job, seconds: float) -> None:
        with self.condition:
            job.done += 1
            self.drawing_since = None
            self.per_image = round(0.8 * self.per_image + 0.2 * seconds, 2)

    def _remove(self, job: Job) -> None:
        if job in self.jobs:
            self.jobs.remove(job)
        self.drawing_since = None
        self.condition.notify_all()

    def finish(self, job: Job) -> None:
        with self.condition:
            self._remove(job)


_queue = Queue()
_ready = False
_classifier: Any = None


def _token() -> bytes:
    with open(os.path.join(SECRET_DIR, "token"), "rb") as handle:
        return handle.read().strip()


def _engine_command() -> list[str]:
    chosen = MODELS[MODEL]
    return [
        "/opt/sd/sd-server",
        "--diffusion-model",
        f"{WEIGHTS}/{chosen['diffusion']}",
        "--vae",
        f"{WEIGHTS}/{chosen['vae']}",
        "--llm",
        f"{WEIGHTS}/{chosen['llm']}",
        "--cfg-scale",
        "1.0",
        "--steps",
        str(chosen["steps"]),
        "-t",
        str(THREADS),
        "--listen-ip",
        "127.0.0.1",
        "--listen-port",
        "7860",
    ]


def _engine_call(path: str, body: dict[str, Any] | None, seconds: float) -> Any:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        ENGINE + path,
        data=data,
        method="GET" if body is None else "POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=seconds) as response:
        return json.load(response)


def _load_classifier() -> None:
    global _classifier
    import torch
    from transformers import AutoImageProcessor, AutoModelForImageClassification

    torch.set_num_threads(4)
    folder = f"{WEIGHTS}/filter"
    processor = AutoImageProcessor.from_pretrained(folder, local_files_only=True)
    model = AutoModelForImageClassification.from_pretrained(
        folder, local_files_only=True, use_safetensors=True
    ).eval()
    labels = {int(k): str(v).lower() for k, v in model.config.id2label.items()}
    if "nsfw" not in labels.values():
        raise SystemExit("classifier labels unexpected")
    _classifier = (torch, processor, model, labels)


def _nsfw(picture: Any) -> float:
    torch, processor, model, labels = _classifier
    with torch.no_grad():
        inputs = processor(images=picture.convert("RGB"), return_tensors="pt")
        probabilities = model(**inputs).logits.softmax(-1)[0].tolist()
    return float(
        sum(p for index, p in enumerate(probabilities) if labels[index] == "nsfw")
    )


def _watch(engine: subprocess.Popen[bytes]) -> None:
    """Readiness, then exit the whole container if the engine dies."""
    global _ready
    started = time.monotonic()
    while engine.poll() is None:
        if not _ready:
            try:
                status = _engine_call("/sdcpp/v1/models/status", None, 5)
                if str(status.get("state", "")).lower() in ("ready", "loaded"):
                    _ready = True
                    print(
                        f"engine ready in {time.monotonic() - started:.1f} s",
                        flush=True,
                    )
            except (OSError, ValueError):
                pass
        time.sleep(1)
    print("engine stopped", engine.returncode, flush=True)
    os._exit(1)


def render(prompt: str, seed: int) -> dict[str, Any]:
    from PIL import Image

    started = time.monotonic()
    body = {
        "prompt": prompt,
        "negative_prompt": "",
        "width": DRAW_PX,
        "height": DRAW_PX,
        "steps": MODELS[MODEL]["steps"],
        "cfg_scale": 1.0,
        "seed": seed,
        "batch_size": 1,
    }
    reply = _engine_call("/sdapi/v1/txt2img", body, ENGINE_SECONDS)
    drawn = time.monotonic()
    picture = Image.open(io.BytesIO(base64.b64decode(reply["images"][0])))
    picture = picture.convert("RGB").resize(
        (SHOW_PX, SHOW_PX), Image.Resampling.LANCZOS
    )
    score = _nsfw(picture)
    buffer = io.BytesIO()
    picture.save(buffer, "PNG", optimize=True)
    return {
        "image": base64.b64encode(buffer.getvalue()).decode("ascii"),
        "nsfw": round(score, 4),
        "render_seconds": round(drawn - started, 2),
        "filter_seconds": round(time.monotonic() - drawn, 2),
        "model": MODEL,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "pictures-gate"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:  # no request content
        print(
            f"{self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}",
            flush=True,
        )

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self) -> bool:
        given = self.headers.get("Authorization", "")
        expected = b"Bearer " + _token()
        if hmac.compare_digest(given.encode(), expected):
            return True
        self._send(401, {"code": "unauthorised"})
        return False

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if not self._authorised():
            return
        if self.path != "/v1/health":
            self._send(404, {"code": "not_found"})
            return
        with _queue.condition:
            status = {
                "ready": _ready,
                "queue": _queue.images(),
                "jobs": len(_queue.jobs),
                "seconds_per_image": _queue.per_image,
                "busy_seconds": round(_queue.busy_seconds(), 1),
                "model": MODEL,
            }
        self._send(200, status)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if not self._authorised():
            return
        if self.path != "/v1/render":
            self._send(404, {"code": "not_found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY_BYTES:
            self._send(413, {"code": "invalid_input"})
            return
        try:
            request = json.loads(self.rfile.read(length))
            prompt = str(request["prompt"]).strip()
            single = "seeds" not in request
            seeds = (
                [int(request["seed"])] if single else [int(s) for s in request["seeds"]]
            )
            wait = request.get("max_wait_seconds")
            max_wait = None if wait is None else float(wait)
        except (ValueError, KeyError, TypeError):
            self._send(400, {"code": "invalid_input"})
            return
        if (
            not prompt
            or len(prompt) > MAX_PROMPT_CHARS
            or not 1 <= len(seeds) <= MAX_SEEDS
            or not all(0 <= seed < 2**31 for seed in seeds)
            or (max_wait is not None and not 1 <= max_wait <= 900)
        ):
            self._send(400, {"code": "invalid_input"})
            return
        if not _ready:
            self._send(503, {"code": "not_ready"})
            return
        job, expected = _queue.admit(len(seeds), max_wait)
        if job is None:
            print(f"busy expected_s={expected:.0f} queue={_queue.images()}", flush=True)
            self._send(
                503,
                {
                    "code": "busy",
                    "expected_seconds": round(expected, 1),
                    "queue": _queue.images(),
                },
            )
            return
        queued = time.monotonic()
        images: list[dict[str, Any]] = []
        try:
            if not _queue.wait_turn(job):
                print("job_dropped caller_gone", flush=True)
                self._send(504, {"code": "expired"})
                return
            waited = time.monotonic() - queued
            for seed in seeds:
                if images and _queue.expired(job):
                    break  # the caller is gone: do not draw the rest
                _queue.start_image()
                started = time.monotonic()
                result = render(prompt, seed)
                _queue.image_done(job, time.monotonic() - started)
                result["seed"] = seed
                images.append(result)
                print(
                    f"render seed={seed} render_s={result['render_seconds']} "
                    f"queue_s={waited:.2f} expected_s={expected:.0f} "
                    f"per_image_s={_queue.per_image} nsfw={result['nsfw']}",
                    flush=True,
                )
        except (OSError, ValueError, KeyError, urllib.error.URLError) as error:
            print("render_failed", type(error).__name__, flush=True)
            if not images:
                self._send(502, {"code": "render_failed"})
                return
        finally:
            _queue.finish(job)
        if single:
            reply = dict(images[0], queue_seconds=round(waited, 2))
        else:
            reply = {
                "images": images,
                "queue_seconds": round(waited, 2),
                "expected_seconds": round(expected, 1),
                "model": MODEL,
            }
        self._send(200, reply)


def main() -> int:
    if MODEL not in MODELS:
        raise SystemExit("unknown model")
    _token()  # refuse to start without the shared token
    _load_classifier()
    engine = subprocess.Popen(_engine_command(), stdout=sys.stdout, stderr=sys.stderr)
    threading.Thread(target=_watch, args=(engine,), daemon=True).start()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(
        os.path.join(SECRET_DIR, "cert.pem"), os.path.join(SECRET_DIR, "key.pem")
    )
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(f"gate listening on {PORT} model={MODEL}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
