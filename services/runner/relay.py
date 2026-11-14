"""Atlas relay: the member's own computer, reached without any inbound port.

The member runs one readable Atlas script on his computer (connect.sh, served
by the instance). It starts upterm, an SSH server inside a user process that
dials out to the instance's relay (uptermd, Apache-2.0). Atlas then reaches the
computer through the relay, with the per-machine Atlas key that the upterm host
accepts alone. No router change, no administrator rights, no service exposed
on the computer; stopping the script (Ctrl-C) ends the access.

Three bindings hold a connection to one member and one computer:
- the relay registers only host keys listed in its gate file, which this
  module writes when a member enrols a computer with a one-time token, and
  rewrites without that key when he removes the computer;
- the upterm host lets in only the computer's Atlas key, kept in the broker;
- the session a computer announces is signed with its enrolled host key.

uptermd reads its gate once at start (upstream README, "Restricting Host
Registration"; spike of 2026-11-14): the relay image restarts it when the gate
changes, and connected hosts register again under the same session (measured:
under 5 seconds). The relay terminates both SSH legs (a guest sees the relay's
host key), so it is Atlas infrastructure and logs metadata only.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
import threading
import time

from services.runner.policy import RunnerError

# What a computer announces: upterm's session user (session id, then the
# encoded relay node in embedded routing), never an option for ssh.
SESSION_USER = re.compile(r"[A-Za-z0-9]{6,64}(?::[A-Za-z0-9+/=_-]{1,256})?")
TARGET_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
HOST_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]{68}")
NAMESPACE = "atlas-connect"
VERSION = "atlas-connect-v1"
# A signed announcement is accepted within this many seconds of its time.
SKEW_S = 300
_lock = threading.Lock()


def enabled() -> bool:
    return bool(os.environ.get("ATLAS_RELAY_PUBLIC_URL"))


def endpoint() -> tuple[str, int]:
    """The relay's SSH listener as the broker reaches it (an internal address)."""
    value = os.environ.get("ATLAS_RELAY_SSH", "")
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise RunnerError("relay_unavailable")
    return host, int(port)


def relay_key() -> str:
    """The relay's public host key, pinned by the broker and by every computer."""
    value = " ".join(os.environ.get("ATLAS_RELAY_HOST_KEY", "").split()[:2])
    if not HOST_KEY.fullmatch(value):
        raise RunnerError("relay_unavailable")
    return value


def host_key(value: str) -> str:
    """A computer's upterm host key as enrolled: `ssh-ed25519 BASE64`."""
    fields = value.split()
    candidate = " ".join(fields[:2])
    if not HOST_KEY.fullmatch(candidate):
        raise RunnerError("invalid_host_key")
    return candidate


def gate_path() -> Path:
    return Path(os.environ.get("ATLAS_RELAY_GATE", "/relay/hosts"))


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".hosts-")
    with os.fdopen(handle, "w") as stream:
        stream.write("".join(line + "\n" for line in lines))
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def admit(target_id: str, key: str) -> None:
    """List a computer's host key in the relay gate (one line per computer)."""
    if not TARGET_ID.fullmatch(target_id):
        raise RunnerError("runner_unauthorized")
    line = host_key(key) + " atlas-" + target_id
    with _lock:
        path = gate_path()
        lines = path.read_text().splitlines() if path.exists() else []
        kept = [item for item in lines if not item.endswith(" atlas-" + target_id)]
        _write(path, kept + [line])


def revoke(target_id: str) -> bool:
    """Remove a computer from the relay gate; True when it was listed."""
    with _lock:
        path = gate_path()
        if not path.exists():
            return False
        lines = path.read_text().splitlines()
        kept = [item for item in lines if not item.endswith(" atlas-" + target_id)]
        if kept != lines:
            _write(path, kept)
        return kept != lines


def message(target_id: str, user: str | None, at: int) -> str:
    """The exact text a computer signs to announce (or end) its session."""
    return "\n".join([VERSION, target_id, user or "-", str(at)]) + "\n"


def verify(
    key: str, text: str, signature: str, now: float | None = None
) -> dict[str, object]:
    """Check a computer's signed announcement against its enrolled host key.

    Returns {target_id, user (None when the computer disconnected), at}. The
    signature is OpenSSH's (ssh-keygen -Y sign, namespace atlas-connect).
    """
    from services.runner.ssh import process

    lines = text.split("\n")
    if len(text) > 1024 or len(lines) != 5 or lines[0] != VERSION or lines[4]:
        raise RunnerError("invalid_announcement")
    target_id, user, at = lines[1], lines[2], lines[3]
    if (
        not TARGET_ID.fullmatch(target_id)
        or not (user == "-" or SESSION_USER.fullmatch(user))
        or not at.isdigit()
    ):
        raise RunnerError("invalid_announcement")
    current = time.time() if now is None else now
    if abs(current - int(at)) > SKEW_S:
        raise RunnerError("announcement_expired")
    if len(signature) > 4096 or "-----BEGIN SSH SIGNATURE-----" not in signature:
        raise RunnerError("invalid_signature")
    with tempfile.TemporaryDirectory() as folder:
        signers = Path(folder) / "signers"
        signers.write_text("atlas-machine " + host_key(key) + "\n")
        signed = Path(folder) / "signature"
        signed.write_text(signature.strip() + "\n")
        try:
            process(
                [
                    "ssh-keygen",
                    "-Y",
                    "verify",
                    "-f",
                    str(signers),
                    "-I",
                    "atlas-machine",
                    "-n",
                    NAMESPACE,
                    "-s",
                    str(signed),
                ],
                content=text.encode(),
                timeout=10,
            )
        except RunnerError:
            raise RunnerError("invalid_signature") from None
    return dict(target_id=target_id, user=None if user == "-" else user, at=int(at))
