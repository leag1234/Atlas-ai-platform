"""Docker isolation and persistent state for owner Code mode workspaces."""

import asyncio
import hashlib
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
from services.orchestrator import work_egress


def enabled() -> bool:
    return (
        os.environ.get("ATLAS_CODE_MODE") == "1"
        or os.environ.get("ATLAS_PROJECT_MODE") == "1"
    ) and os.environ.get("ATLAS_STABLE") != "1"


def work_carry_enabled() -> bool:
    """Keep a Work session's workspace from one turn to the next (option B)."""
    return os.environ.get("ATLAS_WORK_CARRY") == "1"


def work_carry_days() -> int:
    """Carry directories older than this many days expire at workspace prepare."""
    try:
        return max(1, int(os.environ.get("ATLAS_WORK_CARRY_DAYS", "7")))
    except ValueError:
        return 7


def work_enabled() -> bool:
    """Project Work sessions may run: the owner runtime, or the guest runtime opted in.

    The guest opt-in covers only scoped project Work. Legacy code mode still
    follows enabled(). No terminal container mounts the shared workspace volume.
    """
    return enabled() or (
        os.environ.get("ATLAS_STABLE") == "1"
        and os.environ.get("ATLAS_PROJECT_MODE") == "1"
        and os.environ.get("ATLAS_STABLE_PROJECT_WORK") == "1"
    )


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
        self.egress = work_egress.enabled()
        self.proxy_host = work_egress.proxy_host(self.key)

    def available(self) -> bool:
        if self.scoped:
            from services.orchestrator.code_ui import project_mode

            return project_mode()
        return enabled()

    def carry_path(self) -> str:
        return "/workspace/" + self.scope_key + ".carry"

    def carry_script(self, expanded_bytes: int | None) -> str:
        """Trusted root copy of the current generation into the carry directory."""
        return (
            "import os, shutil\n"
            "SKIP={'.git','__pycache__','.pytest_cache','node_modules'}\n"
            "def size(path):\n"
            "    total=0\n"
            "    for parent,directories,files in os.walk(path):\n"
            "        directories[:]=[d for d in directories if d not in SKIP and not os.path.islink(os.path.join(parent,d))]\n"
            "        for name in files:\n"
            "            child=os.path.join(parent,name)\n"
            "            if os.path.islink(child):\n"
            "                continue\n"
            "            try:\n"
            "                total+=os.lstat(child).st_size\n"
            "            except OSError:\n"
            "                pass\n"
            "    return total\n"
            "def copy(source,destination):\n"
            "    os.makedirs(destination, exist_ok=True)\n"
            "    count=0\n"
            "    for parent,directories,files in os.walk(source):\n"
            "        directories[:]=[d for d in directories if d not in SKIP and not os.path.islink(os.path.join(parent,d))]\n"
            "        for name in directories:\n"
            "            child=os.path.join(parent,name)\n"
            "            if not os.path.islink(child):\n"
            "                os.makedirs(os.path.join(destination, os.path.relpath(child, source)), exist_ok=True)\n"
            "        for name in files:\n"
            "            child=os.path.join(parent,name)\n"
            "            if os.path.islink(child):\n"
            "                continue\n"
            "            target=os.path.join(destination, os.path.relpath(child, source))\n"
            "            shutil.copy2(child, target, follow_symlinks=False)\n"
            "            os.chown(target, 1000, 1000, follow_symlinks=False)\n"
            "            count+=1\n"
            "    return count\n"
            "source='/workspace/" + self.key + "'\n"
            "carry='" + self.carry_path() + "'\n"
            "if os.path.lexists(carry):\n"
            "    shutil.rmtree(carry)\n"
            "limit="
            + (str(int(expanded_bytes)) if expanded_bytes is not None else "None")
            + "\n"
            "if os.path.isdir(source) and (limit is None or size(source) <= limit):\n"
            "    copy(source, carry)\n"
            "    print('carried')\n"
            "else:\n"
            "    print('work_carry_dropped')\n"
        )

    def restore_carry_script(self) -> str:
        """Trusted root copy of the carry content over the new generation."""
        return (
            "import os, shutil\n"
            "carry='" + self.carry_path() + "'\n"
            "target='/workspace/" + self.key + "'\n"
            "count=0\n"
            "if os.path.isdir(carry):\n"
            "    for parent,directories,files in os.walk(carry):\n"
            "        for name in directories:\n"
            "            child=os.path.join(parent,name)\n"
            "            if not os.path.islink(child):\n"
            "                os.makedirs(os.path.join(target, os.path.relpath(child, carry)), exist_ok=True)\n"
            "        for name in files:\n"
            "            child=os.path.join(parent,name)\n"
            "            if os.path.islink(child):\n"
            "                continue\n"
            "            destination=os.path.join(target, os.path.relpath(child, carry))\n"
            "            os.makedirs(os.path.dirname(destination), exist_ok=True)\n"
            "            shutil.copy2(child, destination, follow_symlinks=False)\n"
            "            count+=1\n"
            "print(count)\n"
        )

    async def cleanup(self, keep_carry: bool = False) -> None:
        """Remove every worker and scratch generation for this authorized session."""
        if not self.scoped:
            raise ValueError("scoped_cleanup_required")
        await self._stop_egress()
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
        if keep_carry:
            await self._docker(
                [
                    "--user",
                    "0:0",
                    "--cap-add",
                    "CHOWN",
                    "--mount",
                    f"type=volume,src={volume()},dst=/workspace",
                ],
                self.carry_script(self.limits.get("expanded_bytes")),
                b"",
                120,
            )
        else:
            cleanup_source += (
                "carry='" + self.carry_path() + "'\n"
                "if os.path.lexists(carry):\n"
                "    own(carry)\n"
                "    shutil.rmtree(carry)\n"
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
            # A cold container start on a loaded host can exceed 30 s; the turn
            # waits for the workspace to be erased, so allow a bounded 120 s.
            120,
        )
        import shutil

        root = state_dir() / self.scope_key
        if root.exists():
            shutil.rmtree(root)

    async def restore_carry(self) -> int:
        """Copy a kept carry over the new generation directory; return file count."""
        if not self.scoped:
            raise ValueError("scoped_cleanup_required")
        raw = await self._docker(
            # The generation directory belongs to uid 1000 and Work containers keep
            # no capability, so root could not write there: restore as its owner.
            [
                "--user",
                "1000:1000",
                "--mount",
                f"type=volume,src={volume()},dst=/workspace",
            ],
            self.restore_carry_script(),
            b"",
            120,
        )
        return int(raw.decode().strip() or 0)

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
        if self.egress:
            await self._start_egress()
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
            "import os, shutil, time\n"
            "p='/workspace/"
            + self.key
            + "'; os.makedirs(p, exist_ok=True); os.chown(p,1000,1000)\n"
            # Carries expire: a session still active touches its carry directory
            # mtime on every turn, so only abandoned carries age out.
            # HOME and tool caches (Go build cache, npm and pip caches) live in a sibling
            # directory, mounted at /home/atlas, so the workspace itself stays empty for its
            # first snapshot and a compiled standard library survives between commands.
            + "os.makedirs(p + '.home', exist_ok=True); os.chown(p + '.home',1000,1000)\n"
            # Carries sit next to their session scope (org/project/session/workspace.carry,
            # four levels down) and expire: an active session rewrites its carry each turn.
            + "cutoff=time.time()-"
            + str(work_carry_days() * 86400)
            + "\n"
            "for parent, directories, _ in os.walk('/workspace'):\n"
            "    depth=parent[len('/workspace'):].count('/')\n"
            "    for name in list(directories):\n"
            "        carry=os.path.join(parent, name)\n"
            "        if name.endswith('.carry') and not os.path.islink(carry):\n"
            "            directories.remove(name)\n"
            "            if os.path.getmtime(carry) < cutoff:\n"
            "                shutil.rmtree(carry)\n"
            "    if depth >= 3:\n"
            "        directories[:]=[d for d in directories if d.endswith('.carry')]\n",
            b"",
            # Same bounded allowance as cleanup: a cold container start can exceed 30 s.
            120,
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
                "--mount",
                f"type=volume,src={volume()},dst=/home/atlas,volume-subpath={self.key}.home",
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
        args = self.docker_arguments(mount, source, name)
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

    def docker_arguments(
        self, mount: list[str], source: str, name: str | None = None
    ) -> list[str]:
        name = atlas_ports().name("atlas-code-" + uuid4().hex) if name is None else name
        network = (
            work_egress.internal_network_name(atlas_ports().base)
            if self.egress
            else "none"
        )
        memory = "2g" if self.egress else "768m"
        pids_limit = "256" if self.egress else "64"
        environment = ["HOME=/home/atlas"] + (
            [
                "PIP_USER=1",
                "PIP_CACHE_DIR=/home/atlas/pip",
                "npm_config_prefix=/home/atlas/npm",
                "npm_config_cache=/home/atlas/npm-cache",
            ]
            if self.egress
            # Without a network npm would retry the registry until the command times out;
            # offline it answers at once (ENOTCACHED) and still runs scripts and tests.
            else ["npm_config_offline=true"]
        )
        proxy = (
            [
                "HTTP_PROXY=http://" + self.proxy_host + ":3128",
                "HTTPS_PROXY=http://" + self.proxy_host + ":3128",
                "NO_PROXY=localhost,127.0.0.1,::1",
            ]
            if self.egress
            else []
        )
        args = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--name",
            name,
            "--network",
            network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--read-only",
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--cpus",
            "1",
            "--pids-limit",
            pids_limit,
            "--tmpfs",
            # exec: compiled toolchains (go test, go run) build and run binaries in /tmp;
            # Docker mounts a tmpfs noexec unless told otherwise.
            "/tmp:rw,exec,nosuid,nodev,size=128m,uid=1000,gid=1000",
            *(["--label", "atlas.work=" + self.scope_key] if self.scoped else []),
            *mount,
            "--entrypoint",
            "/usr/bin/env",
            IMAGE,
            "-i",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            *environment,
            *proxy,
            "LANG=C.UTF-8",
            "python3",
            "-c",
            source,
        ]
        return args

    def access_log_path(self, log_dir: Path) -> Path:
        return log_dir / (hashlib.sha256(self.key.encode()).hexdigest() + ".log")

    def prepare_access_log(self, log_dir: Path) -> Path:
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Docker bind mounts need an absolute source path; the state directory may be relative.
        log_dir = log_dir.resolve()
        access_log = self.access_log_path(log_dir)
        access_log.touch(exist_ok=True)
        os.chown(access_log, 13, 13)
        os.chmod(access_log, 0o620)
        return access_log

    def egress_proxy_arguments(self, access_log: Path) -> list[str]:
        return [
            "docker",
            "run",
            "-d",
            "--name",
            atlas_ports().name(work_egress.proxy_name(self.key)),
            "--network",
            work_egress.internal_network_name(atlas_ports().base),
            "--network-alias",
            self.proxy_host,
            # Squid cannot switch users without capabilities: run it as its own user.
            "--user",
            "13:13",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--read-only",
            "--memory",
            "128m",
            "--memory-swap",
            "128m",
            "--pids-limit",
            "64",
            "--tmpfs",
            "/tmp:rw,nosuid,size=8m",
            "--tmpfs",
            "/var/spool/squid:rw,noexec,nosuid,size=16m,uid=13,gid=13",
            "--tmpfs",
            "/run:rw,noexec,nosuid,size=1m,uid=13,gid=13",
            "--mount",
            f"type=bind,src={access_log},dst=/var/log/squid/access.log",
            "--label",
            "atlas.work=" + (self.scope_key or self.key),
            "-e",
            "SQUID_CONFIG="
            + work_egress.squid_configuration(self.scope_key or self.key),
            "--entrypoint",
            "/bin/sh",
            work_egress.PROXY_IMAGE,
            "-c",
            "printf '%s' \"$SQUID_CONFIG\" > /tmp/squid.conf && exec squid -N -f /tmp/squid.conf",
        ]

    async def _start_egress(self) -> None:
        internal_network = work_egress.internal_network_name(atlas_ports().base)
        external_network = work_egress.external_network_name(atlas_ports().base)
        created_internal = await asyncio.create_subprocess_exec(
            "docker",
            "network",
            "create",
            "--internal",
            "--label",
            "atlas.owner=work-egress",
            internal_network,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await created_internal.communicate()
        if created_internal.returncode and b"already exists" not in stderr:
            raise RuntimeError("Work egress internal network creation failed")
        created_external = await asyncio.create_subprocess_exec(
            "docker",
            "network",
            "create",
            "--label",
            "atlas.owner=work-egress",
            external_network,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await created_external.communicate()
        if created_external.returncode and b"already exists" not in stderr:
            raise RuntimeError("Work egress upstream network creation failed")
        log_dir = Path(
            os.environ.get(
                "ATLAS_WORK_EGRESS_LOG_DIR",
                state_dir() / "work-egress-logs",
            )
        )
        access_log = self.prepare_access_log(log_dir)
        started = await asyncio.create_subprocess_exec(
            *self.egress_proxy_arguments(access_log),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await started.communicate()
        if started.returncode:
            await self._stop_egress()
            raise RuntimeError(
                "Work egress proxy start failed: "
                + str(redact(stderr.decode("utf-8", "replace")[-500:]))
            )
        connected = await asyncio.create_subprocess_exec(
            "docker",
            "network",
            "connect",
            external_network,
            atlas_ports().name(work_egress.proxy_name(self.key)),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await connected.communicate()
        if connected.returncode:
            await self._stop_egress()
            raise RuntimeError("Work egress upstream connection failed")

    async def _stop_egress(self) -> None:
        if not self.egress:
            return
        name = atlas_ports().name(work_egress.proxy_name(self.key))
        removed = await asyncio.create_subprocess_exec(
            "docker",
            "rm",
            "-f",
            name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await removed.wait()
