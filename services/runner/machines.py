"""Personal machines: strict member input and the key installation command.

A member declares a machine he already administers. Every value that later
reaches an ssh command line or the command shown to the member is accepted by
an allow-list pattern here; nothing is escaped by hand.
"""

from __future__ import annotations

import re
import shlex
from typing import Literal

from pydantic import Field, field_validator, model_validator

from services.runner.contract import Input

# RFC 1123 host name or dotted IPv4 literal. No colon (the transport is IPv4),
# no leading hyphen (never an ssh option), ASCII only.
LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
HOST = re.compile(LABEL + r"(?:\." + LABEL + r")*")
USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")
WORK_ROOT = re.compile(r"/[A-Za-z0-9_./-]+")
FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
KEY_TYPE = re.compile(r"ssh-ed25519")
KEY_BODY = re.compile(r"[A-Za-z0-9+/]{40,1024}={0,2}")
KEY_COMMENT = "atlas"


def valid_host(value: str) -> str:
    if len(value) > 253 or not HOST.fullmatch(value):
        raise ValueError("invalid_host")
    return value


def valid_user(value: str) -> str:
    if not USER.fullmatch(value):
        raise ValueError("invalid_user")
    return value


def valid_port(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError("invalid_port")
    return value


def valid_work_root(value: str) -> str:
    if (
        len(value) > 500
        or not WORK_ROOT.fullmatch(value)
        or value == "/"
        or "//" in value
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise ValueError("invalid_work_root")
    return value.rstrip("/")


def valid_name(value: str) -> str:
    if value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("invalid_name")
    return value


def valid_public_key(value: str) -> str:
    """Normalized `type base64 atlas`; the generated comment is replaced."""
    fields = value.split()
    if (
        len(fields) < 2
        or not KEY_TYPE.fullmatch(fields[0])
        or not KEY_BODY.fullmatch(fields[1])
    ):
        raise ValueError("invalid_public_key")
    return fields[0] + " " + fields[1] + " " + KEY_COMMENT


def install_command(login: str, address: str, number: int, key: str) -> str:
    """The command a member runs in his own terminal to trust the Atlas key.

    Each part is checked again against its allow-list, then the argument
    vector is joined by the standard library; no caller can pass text that
    reaches the member's shell unvalidated.
    """
    line = valid_public_key(key)
    remote = 'mkdir -p ~/.ssh && echo "' + line + '" >> ~/.ssh/authorized_keys'
    argv = ["ssh"]
    if valid_port(number) != 22:
        argv += ["-p", str(number)]
    argv += [valid_user(login) + "@" + valid_host(address), remote]
    return shlex.join(argv)


class Machine(Input):
    name: str = Field(min_length=1, max_length=120)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(min_length=1, max_length=64)
    os: Literal["linux", "macos"] = "linux"
    work_root: str | None = Field(default=None, max_length=500)
    permission_mode: Literal["ask", "rules", "auto"] = "rules"

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return valid_name(value)

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        return valid_host(value)

    @field_validator("user")
    @classmethod
    def _user(cls, value: str) -> str:
        return valid_user(value)

    @field_validator("work_root")
    @classmethod
    def _root(cls, value: str | None) -> str | None:
        return None if value is None else valid_work_root(value)


class MachinePatch(Input):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    permission_mode: Literal["ask", "rules", "auto"] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else valid_name(value)

    @model_validator(mode="after")
    def changed(self) -> "MachinePatch":
        if self.name is None and self.permission_mode is None:
            raise ValueError("empty_patch")
        return self


class MachineVerify(Input):
    reset_host_key: bool = False
