"""POSIX target helper, transmitted as source; never contains credentials.

The detached watchdog survives SSH loss, records process identity before work,
and kills the entire command group on timeout/cancel. No command is replayed.
"""

from __future__ import annotations

import base64
import glob
import hashlib
import json
import os
from pathlib import Path
import signal
import selectors
import subprocess
import sys
import time
from typing import Any

MAX_LOG = 16 * 1024 * 1024
MAX_FETCH = 16 * 1024 * 1024


def process_identity(pid: int) -> str | None:
    if sys.platform == "darwin":
        reply = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            env={"PATH": os.defpath, "LC_ALL": "C"},
            timeout=5,
        )
        return reply.stdout.strip() if reply.returncode == 0 else None
    try:
        # The comm field may contain spaces and parentheses.
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def save(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def inside(root: Path, value: str) -> Path:
    candidate = root / value
    if Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError("unsafe_remote_path")
    for parent in (candidate, *candidate.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("unsafe_remote_path")
    candidate.resolve().relative_to(root.resolve())
    return candidate


def supervise(root: Path, state_dir: Path, data: dict[str, Any]) -> None:
    record = state_dir / "state.json"
    env = {"PATH": "/usr/local/bin:" + os.defpath, "HOME": str(root), "LANG": "C.UTF-8"}
    dot = inside(root, ".atlas/remote.env")
    if dot.is_file():
        if dot.stat().st_size > 65536:
            raise ValueError("remote_env_limit")
        for line in dot.read_text().splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            key, value = line.split("=", 1)
            if not key.isidentifier() or key in {"LD_PRELOAD", "LD_LIBRARY_PATH"}:
                raise ValueError("invalid_remote_env")
            env[key] = value

    with (state_dir / "output.log").open("wb", buffering=0) as log:
        cwd = inside(root, data["cwd"])
        if not cwd.is_dir():
            save(
                record,
                dict(state="failed", exit_code=None, error="remote_cwd_unavailable"),
            )
            return
        child = subprocess.Popen(
            ["/bin/sh", "-c", data["command"]],
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        started = time.time()
        state = dict(
            state="running",
            pid=child.pid,
            started_at=started,
            exit_code=None,
            timeout_s=data["timeout_s"],
            process_start=process_identity(child.pid),
        )
        save(record, state)
        reason, received, eof, last_disk = "", 0, False, started
        if child.stdout is None:
            raise ValueError("missing_output_pipe")
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while True:
                for _, _ in selector.select(0.05):
                    chunk = os.read(child.stdout.fileno(), 65536)
                    if not chunk:
                        eof = True
                        selector.unregister(child.stdout)
                        break
                    room = max(0, MAX_LOG - received)
                    log.write(chunk[:room])
                    received += len(chunk)
                    if received > MAX_LOG:
                        reason = "output_limit"
                if (state_dir / "cancel").exists():
                    reason = "cancelled"
                elif time.time() - started >= data["timeout_s"]:
                    reason = "timed_out"
                if time.time() - last_disk >= 1:
                    last_disk = time.time()
                    size = 0
                    for index, path in enumerate(root.rglob("*")):
                        if not path.is_symlink() and path.is_file():
                            size += path.stat().st_size
                        if size > data["disk_quota_bytes"] or index > 20000:
                            reason = "disk_limit"
                            break
                if reason or child.poll() is not None:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if child.poll() is not None and eof:
                    break
        code = child.wait()
        child.stdout.close()
        state.update(
            state=reason
            if reason in {"cancelled", "timed_out"}
            else "failed"
            if reason or code != 0
            else "succeeded",
            exit_code=code,
            ended_at=time.time(),
        )
        if reason in {"output_limit", "disk_limit"}:
            state["error"] = "runner_" + reason
        save(record, state)


def main(data: dict[str, Any]) -> dict[str, Any]:
    root = Path(data["root"])
    operation = data["operation"]
    control = root.parent / "control"
    if operation == "reports":
        reports: list[dict[str, str]] = []
        size = 0
        for index, path in enumerate(root.rglob("*")):
            if index >= 20000 or len(reports) >= 3:
                break
            if not (
                path.name.endswith(".tap")
                or (
                    path.suffix == ".xml"
                    and ("junit" in path.name or "test" in path.name)
                )
            ):
                continue
            path = inside(root, str(path.relative_to(root)))
            if not path.is_file() or path.stat().st_size + size > 1048576:
                continue
            content = path.read_bytes()
            size += len(content)
            if size > 1048576:
                break
            reports.append(
                dict(name=path.name, content=base64.b64encode(content).decode())
            )
        return dict(reports=reports, bytes_received=size)
    if operation == "fetch":
        pattern = data["path_or_glob"]
        inside(root, pattern)
        files: list[dict[str, Any]] = []
        size = 0
        for name in glob.iglob(str(root / pattern), recursive=True):
            path = inside(root, str(Path(name).relative_to(root)))
            if not path.is_file() or ".git" in path.relative_to(root).parts:
                continue
            size += path.stat().st_size
            if size > MAX_FETCH or len(files) >= 20:
                raise ValueError("remote_fetch_limit")
            content = path.read_bytes()
            if len(content) + sum(f["size_bytes"] for f in files) > MAX_FETCH:
                raise ValueError("remote_fetch_limit")
            files.append(
                dict(
                    path=str(path.relative_to(root)),
                    content=base64.b64encode(content).decode(),
                    size_bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            )
        return dict(files=files)
    state_dir = control / data["command_id"]
    if operation == "launch":
        # mkdir is the remote idempotency boundary. Existing intent is reconciled,
        # never launched a second time even after an ambiguous SSH disconnect.
        control.mkdir(exist_ok=True, mode=0o700)
        try:
            state_dir.mkdir(mode=0o700)
        except FileExistsError:
            return dict(state="unknown")
        save(state_dir / "state.json", dict(state="unknown", exit_code=None))
        pid = os.fork()
        if pid == 0:
            os.setsid()
            for descriptor in (0, 1, 2):
                fd = os.open(os.devnull, os.O_RDWR)
                os.dup2(fd, descriptor)
                if fd > 2:
                    os.close(fd)
            try:
                supervise(root, state_dir, data)
            except (OSError, ValueError):
                save(
                    state_dir / "state.json",
                    dict(
                        state="failed", exit_code=None, error="remote_supervisor_failed"
                    ),
                )
            os._exit(0)
        return dict(state="running")
    if operation == "cancel":
        (state_dir / "cancel").touch()
    state = json.loads((state_dir / "state.json").read_text())
    if state["state"] == "running":
        current_identity = process_identity(state["pid"])
        identity_matches = (
            current_identity is not None and current_identity == state["process_start"]
        )
        expired = time.time() - state["started_at"] >= state.get("timeout_s", 1800)
        if identity_matches and (operation == "cancel" or expired):
            try:
                os.killpg(state["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
            state.update(
                state="cancelled" if operation == "cancel" else "timed_out",
                ended_at=time.time(),
                exit_code=-9,
            )
            save(state_dir / "state.json", state)
        if not identity_matches:
            state["state"] = "unknown"
    log = state_dir / "output.log"
    if log.exists():
        if operation == "log":
            with log.open("rb") as stream:
                content = stream.read(MAX_LOG + 65536)
            state["content"] = base64.b64encode(content).decode()
        else:
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 4096))
                state["output"] = stream.read(4096).decode("utf-8", errors="replace")
        state["bytes_received"] = log.stat().st_size
    state.pop("process_start", None)
    state.pop("pid", None)
    return dict(state)


if __name__ == "__main__":
    try:
        print(json.dumps(main(json.loads(sys.stdin.read()))))
    except (OSError, ValueError, KeyError):
        print(json.dumps(dict(error="remote_operation_failed", state="unknown")))
        sys.exit(1)
