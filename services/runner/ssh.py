"""OpenSSH transport: pinned endpoints and keys confined to the broker volume."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any
from uuid import UUID, uuid4

from services.runner.policy import RunnerError

Json = dict[str, Any]


def environment() -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


def process(
    args: list[str],
    *,
    content: bytes | None = None,
    cwd: Path | None = None,
    timeout: float = 30,
) -> bytes:
    try:
        reply = subprocess.run(
            args,
            input=content,
            cwd=cwd,
            env=environment(),
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RunnerError("runner_connection_unknown") from None
    if reply.returncode:
        if (
            b"Host key verification failed" in reply.stderr
            or b"REMOTE HOST IDENTIFICATION HAS CHANGED" in reply.stderr
        ):
            raise RunnerError("host_key_fingerprint_mismatch")
        if b"Permission denied (publickey)" in reply.stderr:
            raise RunnerError("credential_unavailable")
        raise RunnerError("runner_transport_failed")
    return reply.stdout


def fingerprint(public: str) -> str:
    try:
        fields = public.split()
        raw = base64.b64decode(fields[1], validate=True)
        if not fields[0].startswith(("ssh-", "ecdsa-")) or len(raw) < 32:
            raise ValueError
    except (ValueError, IndexError):
        raise RunnerError("invalid_public_key") from None
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip(
        "="
    )


class Keys:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, org: str, reference: str) -> Path:
        directory = self.root / str(UUID(org))
        directory.mkdir(exist_ok=True, mode=0o700)
        return directory / str(UUID(reference))

    def generate(self, org: str) -> Json:
        reference = str(uuid4())
        path = self.path(org, reference)
        process(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "atlas-runner",
                "-f",
                str(path),
            ]
        )
        public = path.with_suffix(".pub").read_text().strip()
        # No retrieval route exists for public key bytes after this response.
        path.with_suffix(".pub").unlink()
        return dict(
            credential_ref=reference, public_key=public, fingerprint=fingerprint(public)
        )

    def require(self, org: str, reference: str) -> Path:
        path = self.path(org, reference)
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise RunnerError("credential_unavailable")
        return path


class SSH:
    def __init__(
        self, keys: Keys, root: Path, org: str, target: Json, jump: Json | None = None
    ) -> None:
        self.keys, self.org, self.target = keys, org, target
        self.root = root / str(UUID(org)) / str(UUID(target["id"]))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jump = SSH(keys, root, org, jump) if jump else None

    def pin(self) -> Path:
        target = self.target
        from services.runner.network import allow

        if self.jump is None:
            allow(target["host"], target["port"])
        host = (
            target["host"]
            if target["port"] == 22
            else "[" + target["host"] + "]:" + str(target["port"])
        )
        path = self.root / "known_hosts"
        if path.exists():
            saved = path.read_text().splitlines()
            if saved and all(
                line.split()[0] == host
                and fingerprint(" ".join(line.split()[1:]))
                in target["host_key_fingerprints"]
                for line in saved
            ):
                return path
        if self.jump:
            # Scan from the registered jump host; never accept its opinion of
            # trust, only public key bytes matching the administrator's pin.
            raw = self.jump.run(
                "ssh-keyscan -T 5 -p "
                + str(target["port"])
                + " "
                + shlex.quote(target["host"]),
                timeout=15,
            )
        else:
            raw = process(
                [
                    "ssh-keyscan",
                    "-T",
                    "5",
                    "-t",
                    "ed25519",
                    "-p",
                    str(target["port"]),
                    target["host"],
                ],
                timeout=10,
            )
        lines = []
        for line in raw.decode("ascii", errors="replace").splitlines():
            fields = line.split()
            if len(fields) == 3 and not line.startswith("#"):
                public = " ".join(fields[1:])
                if fingerprint(public) in target["host_key_fingerprints"]:
                    lines.append(host + " " + public)
        if not lines:
            raise RunnerError("host_key_fingerprint_mismatch")
        path = self.root / "known_hosts"
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o600)
        return path

    def options(self) -> list[str]:
        known = self.pin()
        private = self.keys.require(self.org, self.target["credential_ref"])
        public = process(["ssh-keygen", "-y", "-f", str(private)]).decode()
        if fingerprint(public) != self.target["credential_fingerprint"]:
            raise RunnerError("credential_fingerprint_mismatch")
        options = [
            "-F",
            "/dev/null",
            "-i",
            str(self.keys.require(self.org, self.target["credential_ref"])),
            "-o",
            "UserKnownHostsFile=" + str(known),
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ForwardX11=no",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "PermitLocalCommand=no",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "AddressFamily=inet",
        ]
        if self.jump:
            transport = [
                "ssh",
                *self.jump.options(),
                "-p",
                str(self.jump.target["port"]),
                "-W",
                self.target["host"] + ":" + str(self.target["port"]),
                self.jump.target["user"] + "@" + self.jump.target["host"],
            ]
            options += ["-o", "ProxyCommand=" + shlex.join(transport)]
        return options

    def run(
        self, command: str, content: bytes | None = None, timeout: float = 30
    ) -> bytes:
        return process(
            [
                "ssh",
                *self.options(),
                "-p",
                str(self.target["port"]),
                self.target["user"] + "@" + self.target["host"],
                command,
            ],
            content=content,
            timeout=timeout,
        )

    def copy(self, local: Path, remote: str) -> None:
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", remote):
            raise RunnerError("invalid_remote_path")
        process(
            [
                "scp",
                *self.options(),
                "-P",
                str(self.target["port"]),
                str(local),
                self.target["user"] + "@" + self.target["host"] + ":" + remote,
            ]
        )

    def probe(self) -> Json:
        import json

        script = (
            "import json,platform,shutil,subprocess; print(json.dumps(dict(os=platform.system().lower(),arch=platform.machine(),free_bytes=shutil.disk_usage("
            + repr(self.target["work_root"])
            + ").free,git=subprocess.check_output(['git','--version'],text=True).strip())))"
        )
        result: Json = json.loads(self.run("python3 -c " + shlex.quote(script)))
        expected = "darwin" if self.target.get("os") == "macos" else "linux"
        if result.get("os") != expected:
            raise RunnerError("unsupported_target_os")
        result["os"] = "macos" if expected == "darwin" else "linux"
        return dict(status="available", **result)
