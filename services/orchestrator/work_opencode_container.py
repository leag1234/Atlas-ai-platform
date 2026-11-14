"""Per-turn opencode engine container for the Work engine (step 1c).

Each Work turn that selects the opencode engine gets its own container
running ``opencode serve`` on the session's internal egress network, next to
the session's egress proxy. The engine reaches the Atlas API through that
proxy (the API host is the only host added to the proxy allowlist for the
engine); the orchestrator reaches the engine by container IP; no host port is
published. Secrets reach docker through its environment, never its argument
list, and are never logged.
"""

import asyncio
import json
import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp

from packages.ports import atlas_ports
from services.orchestrator import work_egress
from services.orchestrator.code_workspace import Workspace, redact, volume
from services.orchestrator.work_opencode import (
    IMAGE,
    SERVER_PORT,
    basic_auth_header,
    provider_configuration,
)

HEALTH_TIMEOUT = 30.0
MEMORY = "1536m"
PIDS_LIMIT = "256"

OPENCODE_DISABLE_VARIABLES = (
    "OPENCODE_DISABLE_AUTOUPDATE=1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD=1",
    "OPENCODE_DISABLE_DEFAULT_PLUGINS=1",
    "OPENCODE_DISABLE_CLAUDE_CODE=1",
    "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT=1",
    "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1",
)
# Names only: docker copies their values from its own environment.
SECRET_VARIABLES = ("OPENCODE_SERVER_PASSWORD", "ATLAS_API_KEY")


@dataclass(frozen=True)
class EngineContainer:
    url: str
    password: str
    name: str


def container_name() -> str:
    return atlas_ports().name("atlas-opencode-" + uuid4().hex)


def api_base() -> str:
    """Public HTTPS base URL of the Atlas API the engine calls (required)."""
    base = os.environ.get("ATLAS_WORK_ENGINE_API_BASE", "").strip()
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.hostname:
        raise RuntimeError("missing_configuration: ATLAS_WORK_ENGINE_API_BASE")
    return base.rstrip("/")


def opencode_config(base: str, provider: str, model_id: str) -> str:
    """OPENCODE_CONFIG_CONTENT: Atlas API as provider, locked-down defaults."""
    configuration = provider_configuration(
        base, "{env:ATLAS_API_KEY}", provider, model_id
    )
    configuration["permission"] = {
        "bash": "ask",
        "edit": "allow",
        "webfetch": "ask",
        "external_directory": "deny",
    }
    configuration["autoupdate"] = False
    configuration["share"] = "disabled"
    return json.dumps(configuration)


def docker_arguments(
    name: str,
    workspace: Workspace,
    network: str,
    config_content: str,
) -> list[str]:
    """The full ``docker run -d`` argument list for one engine container."""
    proxy = "http://" + workspace.proxy_host + ":" + str(work_egress.PROXY_PORT)
    return [
        "docker",
        "run",
        "-d",
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
        MEMORY,
        "--memory-swap",
        MEMORY,
        "--cpus",
        "1",
        "--pids-limit",
        PIDS_LIMIT,
        "--user",
        "1000:1000",
        "--tmpfs",
        # exec: go test and go run build and run binaries in /tmp (tmpfs is noexec by default).
        "/tmp:rw,exec,nosuid,nodev,size=256m,uid=1000,gid=1000",
        "--tmpfs",
        "/home/atlas:rw,nosuid,nodev,size=256m,uid=1000,gid=1000",
        *(["--label", "atlas.work=" + workspace.scope_key] if workspace.scoped else []),
        "--mount",
        "type=volume,src="
        + volume()
        + ",dst=/workspace,volume-nocopy,volume-subpath="
        + workspace.key,
        "--workdir",
        "/workspace",
        "-e",
        "HOME=/home/atlas",
        "-e",
        "HTTP_PROXY=" + proxy,
        "-e",
        "HTTPS_PROXY=" + proxy,
        "-e",
        "NO_PROXY=" + work_egress.NO_PROXY,
        "-e",
        "OPENCODE_CONFIG_CONTENT=" + config_content,
        *(part for variable in SECRET_VARIABLES for part in ("-e", variable)),
        *(part for variable in OPENCODE_DISABLE_VARIABLES for part in ("-e", variable)),
        # The Work image entrypoint starts its own terminal service: bypass it.
        "--entrypoint",
        "opencode",
        IMAGE,
        "serve",
        "--hostname",
        "0.0.0.0",
        "--port",
        str(SERVER_PORT),
    ]


async def _run(
    *args: str, env: dict[str, str] | None = None
) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await process.communicate()
    return process.returncode or 0, stdout, stderr


async def _container_ip(name: str, network: str) -> str:
    code, stdout, stderr = await _run(
        "docker",
        "inspect",
        "--format",
        '{{(index .NetworkSettings.Networks "' + network + '").IPAddress}}',
        name,
    )
    if code:
        raise RuntimeError(
            "opencode_engine_inspect_failed: "
            + str(redact(stderr.decode("utf-8", "replace")[-500:]))
        )
    address = stdout.decode().strip()
    if not address or address == "<no value>":
        raise RuntimeError("opencode_engine_no_address")
    return address


async def _wait_healthy(url: str, password: str, timeout: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    async with aiohttp.ClientSession(trust_env=False) as http:
        while True:
            try:
                async with http.get(
                    url + "/global/health",
                    headers={"Authorization": basic_auth_header(password)},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as response:
                    if response.status == 200:
                        return
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            if loop.time() >= deadline:
                raise RuntimeError("opencode_engine_health_timeout")
            await asyncio.sleep(0.5)


async def start(workspace: Workspace, model_id: str) -> EngineContainer:
    """Start one engine container for a turn and wait for its health endpoint.

    The caller has already prepared the workspace (process_work does it before
    any tool runs): that created the session directory and, with registry
    egress, the session's proxy. Preparing again would start a second proxy
    under the same name and fail the turn.
    """
    if not work_egress.enabled():
        raise RuntimeError("missing_configuration: ATLAS_WORK_EGRESS")
    api_key = os.environ.get("ATLAS_WORK_ENGINE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("missing_configuration: ATLAS_WORK_ENGINE_API_KEY")
    base = api_base()
    password = secrets.token_urlsafe(24)
    name = container_name()
    network = work_egress.internal_network_name(atlas_ports().base)
    env = dict(os.environ)
    env["OPENCODE_SERVER_PASSWORD"] = password
    env["ATLAS_API_KEY"] = api_key
    args = docker_arguments(
        name, workspace, network, opencode_config(base, "atlas", model_id)
    )
    code, _, stderr = await _run(*args, env=env)
    if code:
        await _run("docker", "rm", "-f", name)
        raise RuntimeError(
            "opencode_engine_start_failed: "
            + str(redact(stderr.decode("utf-8", "replace")[-500:]))
        )
    try:
        address = await _container_ip(name, network)
        url = "http://" + address + ":" + str(SERVER_PORT)
        await _wait_healthy(url, password, HEALTH_TIMEOUT)
    except Exception:
        await stop(name)
        raise
    return EngineContainer(url=url, password=password, name=name)


async def stop(name: str) -> None:
    """Remove the engine container. The workspace and its proxy stay."""
    await _run("docker", "rm", "-f", name)
