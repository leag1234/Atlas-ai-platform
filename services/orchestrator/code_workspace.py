"""Docker isolation and persistent state for owner Code mode workspaces."""

import asyncio
import json
import os
from pathlib import Path
import re
from typing import Any
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from packages.ports import atlas_ports
from services.orchestrator.terminal_stack import IMAGE
from services.orchestrator import code_worker


def enabled() -> bool:
    return (
        os.environ.get("ATLAS_CODE_MODE") == "1"
        or os.environ.get("ATLAS_PROJECT_MODE") == "1"
    ) and os.environ.get("ATLAS_STABLE") != "1"


def session_key(value: str) -> str:
    return UUID(value).hex


def volume() -> str:
    return atlas_ports().name("atlas-code-workspaces")


def state_dir() -> Path:
    path = Path(os.environ.get("ATLAS_CODE_STATE_DIR", "BRAIN/code-mode/state"))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def redact(value: Any) -> Any:
    """Remove host credential values and conventional plaintext secret assignments."""
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if re.search(
                r"(?i)(?:^|_)(?:password|secret|api[_-]?key|token|api_token|auth_token|access_token|refresh_token|private_key)$",
                str(key),
            )
            else redact(part)
            for key, part in value.items()
        }
    if isinstance(value, list):
        return [redact(part) for part in value]
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(("{", "[")):
        try:
            structured = json.loads(value)
        except ValueError:
            structured = None
        if isinstance(structured, (dict, list)):
            return json.dumps(redact(structured), ensure_ascii=False)
    for name, secret in os.environ.items():
        if (
            re.search(r"KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL", name, re.I)
            and len(secret) >= 6
        ):
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(
        r"(?i)(\b(?:[\w-]*(?:password|secret|api[_-]?key|token|private_key)[\w-]*)[\"']?\s*[=:]\s*[\"']?)[^\s\"',;]+",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(
        r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|AK[I]A[A-Z0-9]{16})\b",
        "[REDACTED]",
        value,
    )
    value = re.sub(r"(https?://)[^\s/:]+:[^\s/@]+@", r"\1[REDACTED]@", value)
    value = re.sub(
        r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*", r"\1 [REDACTED]", value
    )
    value = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        value,
        flags=re.S,
    )
    return value


class Audit:
    def __init__(self, session: str, turn: str) -> None:
        self.session, self.turn = session, turn
        self.path = state_dir() / (session_key(session) + ".jsonl")
        self.sequence = 0

    def record(self, kind: str, **data: Any) -> dict[str, Any]:
        self.sequence += 1
        event = redact(
            {
                "version": 1,
                "session_id": self.session,
                "turn_id": self.turn,
                "seq": self.sequence,
                "type": kind,
                **data,
            }
        )
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return dict(event)


class Workspace:
    def __init__(
        self,
        session: str,
        *,
        org: str | None = None,
        project: str | None = None,
        workspace: str | None = None,
        generation: int = 1,
        authorize: Callable[[], Awaitable[None]] | None = None,
        limits: dict[str, Any] | None = None,
    ) -> None:
        self.key = session_key(session)
        self.scoped = org is not None
        self.authorize = authorize
        self.limits = limits or {}
        self.scope_key = ""
        if self.scoped:
            if project is None or workspace is None or generation < 1:
                raise ValueError("invalid_workspace_scope")
            self.scope_key = "/".join(
                (
                    session_key(str(org)),
                    session_key(project),
                    self.key,
                    session_key(workspace),
                )
            )
            self.key = self.scope_key + "/g" + str(generation)
        self.metadata_path = state_dir() / (self.key + ".json")
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def available(self) -> bool:
        if self.scoped:
            from services.orchestrator.code_ui import project_mode

            return project_mode()
        return enabled()

    async def cleanup(self) -> None:
        """Remove every worker and scratch generation for this authorized session."""
        if not self.scoped:
            raise ValueError("scoped_cleanup_required")
        listed = await asyncio.create_subprocess_exec(
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=atlas.work=" + self.scope_key,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, _ = await listed.communicate()
        if listed.returncode:
            raise RuntimeError("Workspace worker inventory failed")
        ids = output.decode().split()
        if ids:
            removed = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                *ids,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await removed.wait():
                raise RuntimeError("Workspace worker cleanup failed")
        cleanup_source = (
            "import os, shutil\n"
            "def own(path):\n"
            "    os.chown(path, 0, 0, follow_symlinks=False)\n"
            "    if os.path.isdir(path) and not os.path.islink(path):\n"
            "        os.chmod(path, 0o700)\n"
            "        for child in os.scandir(path): own(child.path)\n"
            "path='/workspace/" + self.scope_key + "'\n"
            "if os.path.lexists(path):\n"
            "    own(path)\n"
            "    shutil.rmtree(path)\n"
        )
        await self._docker(
            [
                "--user",
                "0:0",
                "--cap-add",
                "CHOWN",
                "--mount",
                f"type=volume,src={volume()},dst=/workspace",
            ],
            cleanup_source,
            b"",
            30,
        )
        import shutil

        root = state_dir() / self.scope_key
        if root.exists():
            shutil.rmtree(root)

    def metadata(self) -> dict[str, Any]:
        return (
            dict(json.loads(self.metadata_path.read_text()))
            if self.metadata_path.exists()
            else {}
        )

    def save(self, data: dict[str, Any]) -> None:
        descriptor = os.open(
            self.metadata_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w") as handle:
            json.dump(data, handle)

    async def prepare(self) -> None:
        if not self.available():
            raise ValueError("code_mode_disabled")
        # Trusted bootstrap creates only a session directory in the named volume.
        await self._docker(
            [
                "--user",
                "0:0",
                "--cap-add",
                "CHOWN",
                "--mount",
                f"type=volume,src={volume()},dst=/workspace",
            ],
            "import os; p='/workspace/"
            + self.key
            + "'; os.makedirs(p, exist_ok=True); os.chown(p,1000,1000)",
            b"",
            30,
        )

    async def invoke(
        self, action: str, args: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        if not self.available():
            raise ValueError("code_mode_disabled")
        if self.authorize is not None:
            await self.authorize()
        source = Path(code_worker.__file__).read_text()
        if action in {"read_document", "convert_document", "compare_documents"}:
            from packages import attachment_reader

            reader = Path(attachment_reader.__file__).read_text()
            bootstrap = (
                "import sys, types\n"
                "sys.modules['packages'] = types.ModuleType('packages')\n"
                "reader = types.ModuleType('packages.attachment_reader')\n"
                "sys.modules[reader.__name__] = reader\n"
                f"exec(compile({reader!r}, '<document-reader>', 'exec'), reader.__dict__)\n"
            )
            source = bootstrap + source
        if self.limits:
            # Execute the existing worker definitions, then tighten only its
            # published limits before running the unchanged entry point.
            constants = {
                "MAX_BYTES": "expanded_bytes",
                "MAX_FILES": "archive_entries",
                "FILE_CAP": "text_tool_bytes",
                "OUTPUT_CAP": "command_output_bytes",
            }
            overrides = "\n".join(
                f"{name} = min({name}, {int(self.limits[key])})"
                for name, key in constants.items()
            )
            source = source.replace(
                'if __name__ == "__main__":', overrides + '\nif __name__ == "__main__":'
            )
        raw = await self._docker(
            [
                "--user",
                "1000:1000",
                "--mount",
                f"type=volume,src={volume()},dst=/workspace,volume-subpath={self.key}",
            ],
            source,
            json.dumps({"action": action, "args": args}).encode(),
            timeout,
        )
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("invalid_code_worker_result")
        return result

    async def _docker(
        self, mount: list[str], source: str, payload: bytes, timeout: float
    ) -> bytes:
        name = atlas_ports().name("atlas-code-" + uuid4().hex)
        args = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--name",
            name,
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--read-only",
            "--memory",
            "768m",
            "--memory-swap",
            "768m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=128m,uid=1000,gid=1000",
            *(["--label", "atlas.work=" + self.scope_key] if self.scoped else []),
            *mount,
            "--entrypoint",
            "/usr/bin/env",
            IMAGE,
            "-i",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "HOME=/tmp",
            "LANG=C.UTF-8",
            "python3",
            "-c",
            source,
        ]
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await process.communicate(payload)
            if process.returncode:
                raise RuntimeError(
                    f"Code container failed with exit code {process.returncode}: "
                    + str(redact(stderr.decode("utf-8", "replace")[-2000:]))
                )
            return stdout
        finally:
            # Docker CLI cancellation does not kill a container. Remove explicitly.
            cleanup = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.shield(cleanup.wait())
            if process.returncode is None:
                process.kill()
                await process.wait()
