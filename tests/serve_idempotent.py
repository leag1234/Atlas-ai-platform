"""Explicit disruptive J8 check, excluded from unittest discovery and CI."""

import json
import os
import secrets
from pathlib import Path
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen

from packages.ports import atlas_ports


def ready() -> bool:
    try:
        ports = atlas_ports()
        for port, path in (
            (ports.port("chat_ui"), "/"),
            (ports.port("adapter"), "/v1/models"),
        ):
            with urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
                if response.status != 200:
                    return False
        return True
    except (URLError, TimeoutError, ConnectionError):
        return False


def main() -> None:
    if ready():
        raise RuntimeError("active_stack: stop user sessions before running J8")
    # This journey starts real local services but never invokes search/inference.
    # CI has no deployment secrets file; use a fresh key for its disposable terminal.
    environment = dict(os.environ)
    environment.setdefault("TAVILY_API_KEY", "test-only-startup-no-search")
    environment.setdefault("OPEN_TERMINAL_API_KEY", secrets.token_urlsafe(32))
    processes = []
    with Path("BRAIN/m17-serve.log").open("w") as log:
        try:
            for _ in range(2):
                process = subprocess.Popen(
                    ["make", "serve"], stdout=log, stderr=log, env=environment
                )
                processes.append(process)
                deadline = time.monotonic() + 240
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("serve_exited")
                    if ready() and (_ == 0 or processes[0].poll() == 0):
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("serve_readiness_timeout")
                    time.sleep(1)
            Path("BRAIN/eval/serve-idempotent.json").write_text(
                json.dumps({"J8_second_serve_ready": True}) + "\n"
            )
        finally:
            # Signal the recorded launcher; make does not forward termination.
            import signal

            lock = Path("BRAIN/serve.lock")
            if lock.exists():
                pid = int(lock.read_text())
                command = Path(f"/proc/{pid}/cmdline")
                if (
                    command.exists()
                    and b"services.orchestrator.serving"
                    in command.read_bytes().split(b"\0")
                ):
                    os.kill(pid, signal.SIGTERM)
            for process in processes:
                process.wait(timeout=60)


if __name__ == "__main__":
    main()
