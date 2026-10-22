"""M13 GPU-only worker; no remote inference and no request logging."""

import base64
import importlib
import io
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Timer
from time import monotonic
from typing import Any


class WorkerLimit(ValueError):
    def __init__(self, measured: int, limit: int, unit: str) -> None:
        self.data = {"measured": measured, "limit": limit, "unit": unit}
        super().__init__(f"Image input: {measured} {unit}; limit {limit} {unit}")


def place_pipeline(pipeline: Any, memory_bytes: int) -> None:
    # BF16 weights plus encoders exceed a single L4's usable VRAM.
    if memory_bytes < 40 * 1024**3:
        pipeline.enable_sequential_cpu_offload()
    else:
        pipeline.to("cuda")


def main() -> None:
    torch = importlib.import_module("torch")
    config = json.loads(Path(__file__).with_name("image-model.json").read_text())
    pipeline = importlib.import_module("diffusers").FluxPipeline.from_pretrained(
        config["repository"],
        revision=config["revision"],
        torch_dtype=torch.bfloat16,
    )
    place_pipeline(pipeline, torch.cuda.get_device_properties(0).total_memory)
    lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()

        def do_POST(self) -> None:
            if self.path != "/generate" or not lock.acquire(blocking=False):
                self.send_error(409)
                return
            timer = Timer(180, lambda: os._exit(124))
            try:
                self.connection.settimeout(5)
                length = int(self.headers.get("Content-Length", "0"))
                if length > 16000:
                    raise WorkerLimit(length, 16000, "bytes")
                if not 0 < length <= 16000:
                    raise ValueError("invalid_input")
                data = json.loads(self.rfile.read(length))
                prompt = data["prompt"]
                if isinstance(prompt, str) and len(prompt.strip()) > 2000:
                    raise WorkerLimit(len(prompt.strip()), 2000, "characters")
                if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2000:
                    raise ValueError("invalid_input")
                seed = data.get("seed")
                if seed is None:
                    seed = secrets.randbits(32)
                if type(seed) is not int or not 0 <= seed < 2**32:
                    raise ValueError("invalid_seed")
                steps = data.get("steps", 4)
                if steps not in (4, 8):
                    raise ValueError("invalid_steps")
                tokens = len(
                    pipeline.tokenizer_2(prompt, truncation=False)["input_ids"]
                )
                if tokens > 512:
                    raise WorkerLimit(tokens, 512, "tokens")
                started = monotonic()
                timer.start()  # Kill CUDA work too, even if a client disconnects.
                result = pipeline(
                    prompt,
                    height=1024,
                    width=1024,
                    guidance_scale=0.0,
                    num_inference_steps=steps,
                    max_sequence_length=512,
                    generator=torch.Generator("cuda").manual_seed(seed),
                )
                output = io.BytesIO()
                result.images[0].save(output, format="PNG")
                body = json.dumps(
                    {
                        "image": "data:image/png;base64,"
                        + base64.b64encode(output.getvalue()).decode(),
                        "seed": seed,
                        "steps": steps,
                        "max_sequence_length": 512,
                        "seconds": monotonic() - started,
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except WorkerLimit as exc:
                body = json.dumps(exc.data).encode()
                self.send_response(413)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ValueError, KeyError, TypeError, OSError, RuntimeError):
                self.send_error(502, "image_generation_failed")
            finally:
                timer.cancel()
                lock.release()

    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()


if __name__ == "__main__":
    main()
