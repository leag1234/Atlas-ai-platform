"""Serialize restarts and stop only a matching launcher in this checkout."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import signal
import time
from collections.abc import Iterator


def previous_launchers(root: Path) -> list[int]:
    found = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            if (
                process / "cwd"
            ).resolve() != root or process.stat().st_uid != os.getuid():
                continue
            command = (process / "cmdline").read_bytes().split(b"\0")
            if command[1:3] == [b"-m", b"services.orchestrator.serving"]:
                found.append(int(process.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return found


@contextmanager
def ownership() -> Iterator[None]:
    root = Path.cwd().resolve()
    directory = root / "BRAIN"
    directory.mkdir(exist_ok=True)
    with (directory / "serve.lock").open("a+") as lock:
        stable = os.environ.get("ATLAS_STABLE") == "1"
        previous_owner = None
        # A second start requests graceful shutdown; it never kills arbitrary PIDs.
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if stable:
                raise RuntimeError("stable instance already running; use systemctl")
            lock.seek(0)
            pid = int(lock.read())
            previous_owner = pid
            process = Path(f"/proc/{pid}")
            command = (process / "cmdline").read_bytes().split(b"\0")
            if (
                process / "cwd"
            ).resolve() != root or b"services.orchestrator.serving" not in command:
                raise RuntimeError("serve_owner_mismatch")
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 45
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("serve_shutdown_timeout")
                    time.sleep(0.1)
        # Older launchers did not own a lock. Identify them by module, UID and cwd.
        launchers = previous_launchers(root)
        if stable and launchers:
            raise RuntimeError("stable instance already running; use systemctl")
        for pid in launchers:
            if pid == previous_owner:
                continue  # Already signalled; allow interpreter teardown to finish.
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
        deadline = time.monotonic() + 45
        while previous_launchers(root):
            if time.monotonic() >= deadline:
                raise RuntimeError("legacy_serve_shutdown_timeout")
            time.sleep(0.1)
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
