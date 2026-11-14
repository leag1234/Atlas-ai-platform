"""Front of the CPU picture server: one queue, TLS, a shared token, the filter.

Runs inside the container built by infra/pictures-cpu/Dockerfile. It starts
the image engine (sd-server, listening on the container's loopback only),
loads the content classifier once, and serves two authenticated endpoints:

  GET  /v1/health  -> {"ready", "queue", "model"}
  POST /v1/render  {"prompt", "seed"} -> {"image" (base64 PNG, 768 px),
                    "nsfw" (0..1), "render_seconds", "filter_seconds",
                    "queue_seconds", "model"}

One picture is drawn at a time (the engine uses every core); at most
GATE_MAX_QUEUE requests wait, the next ones get 503 {"code": "busy"} at once
so that the caller can fall back. Sizes, steps and guidance are fixed here,
never taken from the request. The engine and the classifier run offline:
the container has no outbound network (install.sh).
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

ENGINE = "http://127.0.0.1:7860"
PORT = int(os.environ.get("GATE_PORT", "8790"))
SECRET_DIR = os.environ.get("GATE_SECRET_DIR", "/run/pictures-secret")
WEIGHTS = os.environ.get("GATE_WEIGHTS", "/w")
MODEL = os.environ.get("GATE_MODEL", "klein")
THREADS = int(os.environ.get("GATE_THREADS", str(os.cpu_count() or 4)))
MAX_QUEUE = int(os.environ.get("GATE_MAX_QUEUE", "6"))
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

_render_lock = threading.Lock()
_state_lock = threading.Lock()
_waiting = 0
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
        self._send(200, {"ready": _ready, "queue": _waiting, "model": MODEL})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        global _waiting
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
            seed = int(request["seed"])
        except (ValueError, KeyError, TypeError):
            self._send(400, {"code": "invalid_input"})
            return
        if not prompt or len(prompt) > MAX_PROMPT_CHARS or not 0 <= seed < 2**31:
            self._send(400, {"code": "invalid_input"})
            return
        if not _ready:
            self._send(503, {"code": "not_ready"})
            return
        with _state_lock:
            if _waiting >= MAX_QUEUE:
                self._send(503, {"code": "busy", "queue": _waiting})
                return
            _waiting += 1
        queued = time.monotonic()
        try:
            with _render_lock:
                waited = time.monotonic() - queued
                result = render(prompt, seed)
        except (OSError, ValueError, KeyError, urllib.error.URLError) as error:
            print("render_failed", type(error).__name__, flush=True)
            self._send(502, {"code": "render_failed"})
            return
        finally:
            with _state_lock:
                _waiting -= 1
        result["queue_seconds"] = round(waited, 2)
        print(
            f"render seed={seed} render_s={result['render_seconds']} "
            f"queue_s={result['queue_seconds']} nsfw={result['nsfw']}",
            flush=True,
        )
        self._send(200, result)


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
