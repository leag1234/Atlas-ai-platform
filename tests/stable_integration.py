"""Disposable real stable stack/front door; only provider transport is synthetic.

Run with ATLAS_PORT_BASE=1900 and PYTHONPATH=.:tests:services/model-gateway.
Secrets exist only in memory and the disposable nginx container, never on disk
in the checkout. All containers, test volumes and the tunnel are removed on exit.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from typing import Any
from unittest.mock import patch

from agent_provider import AgentProvider, AgentRequest
import stream_transport
from serverless_support import environment
from packages.ports import atlas_ports
from services.orchestrator.serving import require_stable_configuration, stack, docker
from services.orchestrator.terminal_stack import (
    terminal,
    user_interface,
    loopback_forward,
)
from services.orchestrator.tools import Runtime, Fetch
from services.orchestrator.cache import Cache
from decimal import Decimal

OUTPUT = Path("BRAIN/stable-evidence")
NGINX = "atlas-stable-integration-nginx-1900"
observed_tools: list[list[str]] = []


def answer(request: AgentRequest) -> dict[str, Any]:
    names = []
    for tool in request.tools:
        function = tool.get("function")
        assert isinstance(function, dict)
        names.append(str(function["name"]))
    observed_tools.append(names)
    assert not {"terminal_command", "publish_document"}.intersection(names)
    text = (
        "Bonjour means hello in French. It is a friendly greeting used during the day."
    )
    if request.produces_files:
        text = json.dumps(
            {
                "title": "Blue squares",
                "slides": [
                    {
                        "title": "Blue squares",
                        "bullets": [
                            "A square has four equal sides.",
                            "All four angles are right angles.",
                        ],
                    },
                    {
                        "title": "Color",
                        "bullets": [
                            "Blue is the chosen fill color.",
                            "A contrasting border separates the shape from its background.",
                        ],
                    },
                    {
                        "title": "Uses",
                        "bullets": [
                            "Blue squares can mark items on a diagram.",
                            "Consistent sizes make a diagram easier to read.",
                        ],
                    },
                ],
            }
        )
    return {
        "text": text,
        "calls": [],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 150},
        "finish_reason": "stop",
    }


async def synthetic_stream(
    request: AgentRequest, model: str, timeout: float
) -> AsyncGenerator[dict[str, object], None]:
    result = answer(request)
    text = str(result["text"])
    for position in range(0, len(text), 20):
        yield {"delta": {"content": text[position : position + 20]}}
        await asyncio.sleep(0.03)
    yield {"result": result}


async def synthetic_complete(
    self: AgentProvider, request: AgentRequest, model: str, *args: Any
) -> dict[str, Any]:
    return answer(request)


def write_container(path: str, data: bytes, mode: str = "640") -> None:
    subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            NGINX,
            "sh",
            "-c",
            'umask 077; cat > "$1"; chown root:www-data "$1"; chmod "$2" "$1"',
            "sh",
            path,
            mode,
        ],
        input=data,
        check=True,
        capture_output=True,
    )


def start_nginx() -> None:
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            NGINX,
            "--network",
            "host",
            "--entrypoint",
            "sh",
            "--mount",
            f"type=bind,src={Path('infra/stable/nginx').resolve()},dst=/atlas-config,readonly",
            "--mount",
            f"type=bind,src={Path('infra/stable/static').resolve()},dst=/usr/share/nginx/html,readonly",
            "nginx:1.18-alpine",
            "-c",
            "sleep 3600",
        ],
        check=True,
        capture_output=True,
    )
    docker(
        "exec",
        NGINX,
        "sh",
        "-c",
        "getent group www-data >/dev/null || addgroup www-data; mkdir -p /etc/letsencrypt/live/atlas.indidea.org",
    )
    write_container(
        "/etc/nginx/atlas-proxy-secret.conf",
        (
            'proxy_set_header X-Atlas-Proxy-Secret "'
            + os.environ["ATLAS_PROXY_SECRET"]
            + '";\n'
        ).encode(),
    )
    entries = []
    for prefix in ("ATLAS_STABLE_BASIC", "ATLAS_STABLE_BOB"):
        hashed = (
            subprocess.run(
                ["openssl", "passwd", "-apr1", "-stdin"],
                input=os.environ[prefix + "_PASSWORD"].encode(),
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        entries.append(os.environ[prefix + "_USER"] + ":" + hashed)
    write_container("/etc/nginx/atlas.htpasswd", ("\n".join(entries) + "\n").encode())
    # Syntax-check the unmodified production server block with disposable certs.
    key_fd, cert_fd = (
        os.memfd_create("stable-test-key"),
        os.memfd_create("stable-test-cert"),
    )
    try:
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-subj",
                "/CN=atlas.indidea.org",
                "-days",
                "1",
                "-keyout",
                f"/proc/self/fd/{key_fd}",
                "-out",
                f"/proc/self/fd/{cert_fd}",
            ],
            pass_fds=(key_fd, cert_fd),
            capture_output=True,
            check=True,
        )
        for fd, filename, mode in (
            (key_fd, "privkey.pem", "600"),
            (cert_fd, "fullchain.pem", "644"),
        ):
            os.lseek(fd, 0, os.SEEK_SET)
            write_container(
                "/etc/letsencrypt/live/atlas.indidea.org/" + filename,
                os.read(fd, 16384),
                mode,
            )
    finally:
        os.close(key_fd)
        os.close(cert_fd)
    for filename, include in (
        ("production", "atlas.indidea.org.conf"),
        ("test", "atlas.test.conf"),
    ):
        write_container(
            "/etc/nginx/" + filename + ".conf",
            (
                "user nginx www-data; events {} http { include /etc/nginx/mime.types; access_log off; include /atlas-config/"
                + include
                + "; }\n"
            ).encode(),
            "644",
        )
        subprocess.run(
            [
                "docker",
                "exec",
                NGINX,
                "nginx",
                "-t",
                "-c",
                "/etc/nginx/" + filename + ".conf",
            ],
            check=True,
        )
    version = subprocess.run(
        ["docker", "exec", NGINX, "nginx", "-v"],
        capture_output=True,
        text=True,
        check=True,
    ).stderr.strip()
    assert "nginx/1.18." in version
    permissions = docker(
        "exec", NGINX, "stat", "-c", "%U:%G:%a", "/etc/nginx/atlas-proxy-secret.conf"
    )
    assert permissions == "root:www-data:640"
    (OUTPUT / "nginx.json").write_text(
        json.dumps(
            {
                "version": version,
                "production_syntax": True,
                "test_syntax": True,
                "include_owner_group_mode": permissions,
            },
            indent=2,
        )
    )
    docker("exec", NGINX, "nginx", "-c", "/etc/nginx/test.conf")


def curl_checks() -> None:
    base = os.environ["ATLAS_STABLE_BASE_URL"].rstrip("/")
    auth = (
        os.environ["ATLAS_STABLE_BASIC_USER"]
        + ":"
        + os.environ["ATLAS_STABLE_BASIC_PASSWORD"]
    )

    def curl(
        url: str, authenticated: bool = False, extra: list[str] | None = None
    ) -> str:
        config = ('user = "' + auth + '"\n') if authenticated else ""
        return subprocess.run(
            [
                "curl",
                "--config",
                "-",
                "--silent",
                "--show-error",
                "--max-time",
                "15",
                *(extra or []),
                url,
            ],
            input=config,
            text=True,
            capture_output=True,
            check=True,
        ).stdout

    assert curl(base, extra=["-o", "/dev/null", "-w", "%{http_code}"]) == "401"
    assert (
        curl(
            atlas_ports().url("chat_ui"),
            extra=[
                "-H",
                "X-Atlas-User: alice@atlas.test",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
            ],
        )
        == "403"
    )
    result = json.loads(
        curl(
            base + "/api/v1/auths/signin",
            True,
            [
                "-H",
                "Content-Type: application/json",
                "-H",
                "X-Atlas-User: forged@example.invalid",
                "-H",
                "X-Atlas-Proxy-Secret: forged",
                "-H",
                "X-Atlas-User-Role: admin",
                "--data",
                '{"email":"ignored","password":"ignored"}',
            ],
        )
    )
    assert result["email"] == "alice@atlas.test" and result["role"] == "user"
    (OUTPUT / "curl.json").write_text(
        json.dumps(
            {
                "anonymous": 401,
                "direct_relay": 403,
                "forged_identity_seen": result["email"],
                "forged_role_seen": result["role"],
            },
            indent=2,
        )
    )


async def private_fetch_checks() -> None:
    runtime = Runtime(Cache(OUTPUT / "private-fetch.sqlite"), Decimal(0))
    for host in (
        "127.0.0.1",
        "169.254.169.254",
        "10.1.2.3",
        "open-terminal",
        "atlas-chat-ui-1900",
        "atlas-chat-1900-db",
    ):
        try:
            await runtime.fetch(Fetch(url="http://" + host + "/"), 5)
        except ValueError as exc:
            assert str(exc) == "ssrf_denied"
        else:
            raise AssertionError("private fetch allowed")


def main() -> None:
    assert atlas_ports().base == 1900, "this disposable harness requires base 1900"
    for port in (4900, 4910, 8900, 9900, 9910, 9911, 9920):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    test_env = {
        **environment(),
        "ATLAS_STABLE": "1",
        "ATLAS_TRUSTED_EMAIL_HEADER": "X-Atlas-User",
        "WEBUI_SECRET_KEY": secrets.token_hex(32),
        "ATLAS_PROXY_SECRET": secrets.token_hex(32),
        "OPEN_TERMINAL_API_KEY": secrets.token_hex(32),
        "ATLAS_TERMINAL_ENABLED": "1",
        "GPU_LOCAL": "0",
        "ATLAS_SEARCH_PROVIDER": "serpapi",
        "SERPAPI_KEY": "synthetic-only",
        "ATLAS_STABLE_BASIC_USER": "alice",
        "ATLAS_STABLE_BASIC_PASSWORD": secrets.token_hex(16),
        "ATLAS_STABLE_BOB_USER": "bob",
        "ATLAS_STABLE_BOB_PASSWORD": secrets.token_hex(16),
        "ATLAS_STABLE_BASE_URL": "http://127.0.0.1:8900/",
        "ATLAS_STABLE_REPORT_DIR": str(OUTPUT),
        "ATLAS_WEB_CACHE": str(OUTPUT / "cache.sqlite"),
        "ATLAS_PROJECT_DB": str(OUTPUT / "projects.sqlite"),
        "ATLAS_IMAGE_DIR": str(OUTPUT / "images"),
        "ATLAS_INTERACTION_DIR": str(OUTPUT / "synthetic-log"),
    }
    try:
        with patch.dict(os.environ, test_env), ExitStack() as contexts:
            require_stable_configuration()
            contexts.enter_context(
                patch.object(stream_transport, "attempt", synthetic_stream)
            )
            contexts.enter_context(
                patch.object(AgentProvider, "_complete", synthetic_complete)
            )
            asyncio.run(private_fetch_checks())
            contexts.enter_context(terminal("atlas-open-terminal", "127.0.0.1:9900"))
            contexts.enter_context(stack("atlas-stable-integration", False))
            contexts.enter_context(user_interface("atlas-stable-integration-ui", False))
            start_nginx()
            with loopback_forward("127.0.0.1:4910", ("127.0.0.1", 4900)):
                curl_checks()
                subprocess.run(
                    [sys.executable, "tests/stable_front_door_browser.py"], check=True
                )
            subprocess.run(
                [sys.executable, "tests/stable_front_door_browser.py", "--maintenance"],
                check=True,
            )
            assert observed_tools and any(
                "web_fetch" in names for names in observed_tools
            )
            (OUTPUT / "tools.json").write_text(
                json.dumps(
                    {
                        "provider": "synthetic test transport",
                        "request_tools": observed_tools,
                        "private_fetches_denied": True,
                    },
                    indent=2,
                )
            )
            log_dir = OUTPUT / "synthetic-log"
            assert log_dir.stat().st_mode & 0o777 == 0o700
            assert all(
                path.stat().st_mode & 0o777 == 0o600 for path in log_dir.glob("*.jsonl")
            )
    finally:
        subprocess.run(["docker", "rm", "-f", NGINX], capture_output=True)
        subprocess.run(
            ["docker", "volume", "rm", "atlas-terminal-files-1900"], capture_output=True
        )
        subprocess.run(
            ["docker", "network", "rm", "atlas-files-1900"], capture_output=True
        )
    print(
        "Stable integration acceptance complete (synthetic provider transport).",
        flush=True,
    )


if __name__ == "__main__":
    main()
