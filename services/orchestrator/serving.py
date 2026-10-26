"""One-command local CPU stack; cloud provisioning is deliberately absent.

The deployed stable instance accepts SIGINT/SIGTERM only from systemd (PID 1).
It waits for those signals with ``sigwaitinfo`` so an ignored request can be
attributed without exposing its environment, and exits non-zero if its stack
ends without an accepted stop so systemd can restart it.
"""

import os
import json
import signal
import secrets
import subprocess
import threading
import time
from contextlib import contextmanager, nullcontext
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

# Block before importing service dependencies, which may initialize worker threads.
# The dedicated waiter below receives these signals only for the deployed instance.
if os.environ.get("ATLAS_STABLE") == "1":
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})

from packages.configuration import CHAT_REQUIRED_ENV, require_env
from packages.ports import atlas_ports
from services.orchestrator.code_ui import project_mode
from services.orchestrator.webui import configure
from services.orchestrator.tools import search_providers

import uvicorn
from gateway_cpu import CPUModels
from http_gateway import serve

from services.retrieval.pipeline import ingest
from services.retrieval.search import Gateway
from services.retrieval.store import Store

WEBUI_IMAGE = "ghcr.io/open-webui/open-webui:v0.11.3@sha256:41daa0cf2561a5d4c8d1ff31ee2a98d93ab4d3ac2605cac69366ff6a3374a933"
SEARXNG_IMAGE = "searxng/searxng:2026.10.17-1c10abe2d@sha256:8bdd9aa136b2dc3b9332cf24714fa34a375b8a1949a1201ec900c24d7378596e"

# Open WebUI v0.11.3's shipped shell references these paths. Keep its shell and
# license notices untouched; these read-only mounts provide Atlas artwork there.
BRAND_ASSET_MOUNTS = (
    ("assets/brand/png/atlas-symbol-512.png", "/app/build/favicon.png"),
    ("assets/brand/png/atlas-symbol-512.png", "/app/build/static/favicon.png"),
    ("assets/brand/png/atlas-symbol-128.png", "/app/build/static/favicon-96x96.png"),
    ("assets/brand/svg/atlas-symbol.svg", "/app/build/static/favicon.svg"),
    (
        "services/orchestrator/static/brand/atlas-symbol.ico",
        "/app/build/static/favicon.ico",
    ),
    ("assets/brand/png/atlas-symbol-256.png", "/app/build/static/apple-touch-icon.png"),
    ("assets/brand/png/atlas-symbol-512.png", "/app/build/static/logo.png"),
    ("assets/brand/png/atlas-symbol-512.png", "/app/build/static/splash.png"),
    ("assets/brand/png/atlas-symbol-dark-512.png", "/app/build/static/splash-dark.png"),
    (
        "assets/brand/png/atlas-symbol-256.png",
        "/app/build/static/web-app-manifest-192x192.png",
    ),
    (
        "assets/brand/png/atlas-symbol-512.png",
        "/app/build/static/web-app-manifest-512x512.png",
    ),
    ("services/orchestrator/static/brand/manifest.json", "/app/build/manifest.json"),
)


def stable_mode() -> bool:
    """Whether this process is the separately deployed stable instance."""
    return os.environ.get("ATLAS_STABLE") == "1"


def stt_enabled() -> bool:
    """Keep guest audio processing opt-in because audio is personal data."""
    return os.environ.get("ATLAS_STT") == "1" and (
        not stable_mode() or os.environ.get("ATLAS_STABLE_STT") == "1"
    )


def stable_stop_allowed(sender_pid: int, stack_failed: bool) -> bool:
    """Accept systemd stops, or allow shutdown while our stack is already failing."""
    return sender_pid == 1 or stack_failed


def sender_command_line(sender_pid: int) -> str:
    """Return a bounded command line for a signal sender which still exists."""
    try:
        return (
            Path(f"/proc/{sender_pid}/cmdline")
            .read_bytes()
            .replace(b"\0", b" ")
            .decode("utf-8", "replace")[:120]
        )
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return ""


def log_json(event: str, **fields: object) -> None:
    """Emit one structured operational event without process environment data."""
    print(
        json.dumps(
            {
                "event": event,
                **fields,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=True,
        ),
        flush=True,
    )


def install_stop_handling(
    stopping: threading.Event, stack_failed: threading.Event
) -> None:
    """Install legacy handlers, or a sender-aware waiter for the stable service."""
    signals = {signal.SIGINT, signal.SIGTERM}
    if not stable_mode():
        for sig in signals:
            signal.signal(sig, lambda _signum, _frame: stopping.set())
        return

    signal.pthread_sigmask(signal.SIG_BLOCK, signals)
    waiter_ready = threading.Event()

    def wait_for_stop() -> None:
        waiter_ready.set()
        while True:
            received = signal.sigwaitinfo(signals)
            sender_pid = received.si_pid
            if stable_stop_allowed(sender_pid, stack_failed.is_set()):
                stopping.set()
                return
            log_json(
                "stop_ignored",
                signal=signal.Signals(received.si_signo).name,
                sender_pid=sender_pid,
                sender_uid=received.si_uid,
                sender_command_line=sender_command_line(sender_pid),
            )

    threading.Thread(target=wait_for_stop, daemon=True).start()
    waiter_ready.wait()


def require_accepted_stop(
    stopping: threading.Event, stack_failed: threading.Event
) -> None:
    """Make a stack that returned by itself visible to systemd as a failure."""
    if not stopping.is_set():
        stack_failed.set()
        log_json("stack_stopped", reason="stack_context_ended")
        raise RuntimeError("stack_context_ended")


def require_stable_configuration() -> None:
    """Fail before Docker side effects when stable authentication is incomplete."""
    if project_mode():
        require_env(("ATLAS_PROJECT_IDENTITY_SECRET",))
        if len(os.environ["ATLAS_PROJECT_IDENTITY_SECRET"].encode()) < 32:
            raise RuntimeError("invalid_configuration: ATLAS_PROJECT_IDENTITY_SECRET")
    if stable_mode():
        require_env(
            ("WEBUI_SECRET_KEY", "ATLAS_TRUSTED_EMAIL_HEADER", "ATLAS_PROXY_SECRET")
        )
        if os.environ["ATLAS_TRUSTED_EMAIL_HEADER"] != "X-Atlas-User":
            raise RuntimeError("invalid_configuration: ATLAS_TRUSTED_EMAIL_HEADER")


def stable_webui_options() -> list[str]:
    """Pinned Open WebUI trusted-header settings, absent outside stable mode.

    Open WebUI v0.11.3 creates trusted-header users through signup_handler.  Its
    first user is provisionally an admin, so the relay supplies the fixed trusted
    role header and Open WebUI immediately changes it to the ordinary user role.
    """
    if not stable_mode():
        return []
    return [
        "-e",
        "WEBUI_AUTH=True",
        "-e",
        "WEBUI_AUTH_TRUSTED_EMAIL_HEADER=" + os.environ["ATLAS_TRUSTED_EMAIL_HEADER"],
        "-e",
        "WEBUI_AUTH_TRUSTED_ROLE_HEADER=X-Atlas-User-Role",
        "-e",
        "ENABLE_SIGNUP=False",
        "-e",
        "ENABLE_LOGIN_FORM=False",
        "-e",
        "ENABLE_INITIAL_ADMIN_SIGNUP=False",
        "-e",
        "ENABLE_CODE_EXECUTION=False",
        "-e",
        "ENABLE_CODE_INTERPRETER=False",
        "-e",
        "ENABLE_FORWARD_USER_INFO_HEADERS=True",
        "-e",
        "WEBUI_SECRET_KEY",
    ]


def docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("docker_operation_failed:" + args[0])
    return result.stdout.strip()


def docker_run(name: str, image: str, options: list[str]) -> None:
    existing = docker(
        "ps", "-a", "--filter", "name=^/" + name + "$", "--format", "{{.Names}}"
    )
    if existing:
        actual = docker("inspect", "--format", "{{.Config.Image}}", name)
        if actual != image:
            raise RuntimeError("container_owner_mismatch")
        try:
            docker("rm", "-f", name)
        except RuntimeError:
            # --rm may already be removing a gracefully stopped container.
            deadline = time.monotonic() + 5
            while docker(
                "ps", "-a", "--filter", "name=^/" + name + "$", "--format", "{{.Names}}"
            ):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
    docker("run", "--rm", "-d", "--name", name, *options, image)


def webui(name: str, persistent: bool) -> None:
    from services.orchestrator.terminal_stack import NETWORK, network_gateway

    gateway = network_gateway()
    ports = atlas_ports()
    name = ports.name(name)
    os.environ["TERMINAL_SERVER_CONNECTIONS"] = json.dumps(
        [
            {
                "id": "atlas-files",
                "url": "http://open-terminal:" + str(ports.port("terminal")),
                "key": os.environ["OPEN_TERMINAL_API_KEY"],
                "auth_type": "bearer",
                "config": (
                    {
                        "chat_uploads": "default",
                        "access_grants": [
                            {
                                "principal_type": "user",
                                "principal_id": "*",
                                "permission": "read",
                            }
                        ],
                    }
                    if stable_mode()
                    else {"chat_uploads": "filesystem"}
                ),
                "enabled": True,
            }
        ]
    )
    options = [
        "--network",
        ports.name(NETWORK),
        "--env-file",
        "infra/chat-ui.env",
        "-e",
        "HOST=0.0.0.0",
        "-e",
        "PORT=" + str(ports.port("chat_ui")),
        "-e",
        "WEBUI_NAME=Atlas",
        "-e",
        "ENABLE_VERSION_UPDATE_CHECK=False",
        "-e",
        "ENABLE_EVALUATION_ARENA_MODELS=False",
        "-e",
        "ATLAS_REMOVE_OPEN_WEBUI_SUFFIX",
        "-e",
        "TERMINAL_SERVER_CONNECTIONS",
        "-e",
        f"OPENAI_API_BASE_URL=http://{gateway}:{ports.port('adapter')}/v1",
        "-e",
        "ENABLE_TITLE_GENERATION=True",
        "-e",
        f"ATLAS_ADAPTER_URL=http://{gateway}:{ports.port('adapter')}",
    ]
    if os.environ.get("ATLAS_TTS") == "1":
        options.extend(
            [
                "-e",
                "AUDIO_TTS_ENGINE=openai",
                "-e",
                f"AUDIO_TTS_OPENAI_API_BASE_URL=http://{gateway}:{ports.port('tts')}/v1",
                "-e",
                "AUDIO_TTS_OPENAI_API_KEY=atlas-local",
                "-e",
                "AUDIO_TTS_MODEL=atlas-tts",
                "-e",
                "AUDIO_TTS_VOICE=atlas-language-routing",
                "-e",
                "AUDIO_TTS_SPLIT_ON=punctuation",
            ]
        )
    if stt_enabled():
        options.extend(
            [
                "-e",
                "ENABLE_VOICE_INTERACTION=True",
                "-e",
                "AUDIO_STT_ENGINE=openai",
                "-e",
                f"AUDIO_STT_OPENAI_API_BASE_URL=http://{gateway}:{ports.port('stt')}/v1",
                "-e",
                "AUDIO_STT_OPENAI_API_KEY=atlas-local",
                "-e",
                "AUDIO_STT_MODEL=atlas-stt",
            ]
        )
    options.extend(stable_webui_options())
    if stable_mode():
        options.extend(
            [
                "-v",
                str(Path("services/orchestrator/stable-auth.js").resolve())
                + ":/app/backend/open_webui/static/atlas/stable-auth.js:ro",
            ]
        )
    options.extend(
        [
            "-v",
            str(Path("services/orchestrator/chat-ui.css").resolve())
            + ":/app/backend/open_webui/static/custom.css:ro",
            "-v",
            str(Path("services/orchestrator/chat-ui.css").resolve())
            + ":/app/backend/open_webui/static/atlas/chat-ui.css:ro",
            "-v",
            str(Path("services/orchestrator/chat-ui.js").resolve())
            + ":/app/backend/open_webui/static/atlas/chat-ui.js:ro",
            "-v",
            str(Path("services/orchestrator/brand-ui.js").resolve())
            + ":/app/backend/open_webui/static/atlas/brand-ui.js:ro",
            "-v",
            str(Path("assets/brand").resolve())
            + ":/app/backend/open_webui/static/atlas/brand:ro",
            "-v",
            str(Path("services/orchestrator/static/nearby.css").resolve())
            + ":/app/backend/open_webui/static/atlas/nearby.css:ro",
            "-v",
            str(Path("services/orchestrator/static/fonts").resolve())
            + ":/app/backend/open_webui/static/atlas/fonts:ro",
            "-v",
            str(Path("services/orchestrator/static/vendor").resolve())
            + ":/app/backend/open_webui/static/atlas/vendor:ro",
        ]
    )
    for source, target in BRAND_ASSET_MOUNTS:
        options.extend(["-v", str(Path(source).resolve()) + ":" + target + ":ro"])
    for resource in (
        "packages/__init__.py",
        "packages/language.py",
        "packages/turn_attachments.py",
        "prompts/activity-labels.json",
    ):
        options.extend(
            ["-v", str(Path(resource).resolve()) + ":/app/backend/" + resource + ":ro"]
        )
    if persistent:
        options.extend(["-v", ports.name("atlas-chat-ui") + ":/app/backend/data"])
    docker_run(name, WEBUI_IMAGE, options)


def wait_http(url: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (URLError, TimeoutError, ConnectionError):
            time.sleep(0.2)
    raise RuntimeError("service_start_timeout")


@contextmanager
def searxng() -> Iterator[None]:
    """Run the opt-in, loopback-only SearXNG sidecar for this stack."""
    ports = atlas_ports()
    name = ports.name("atlas-searxng")
    secret = os.environ.get("SEARXNG_SECRET") or secrets.token_urlsafe(32)
    options = [
        "-p",
        f"127.0.0.1:{ports.port('searxng')}:8080",
        "-v",
        str(Path("infra/searxng/settings.yml").resolve())
        + ":/etc/searxng/settings.yml:ro",
        "-v",
        ports.name("atlas-searxng-data") + ":/var/cache/searxng",
        "-e",
        "SEARXNG_SECRET=" + secret,
        "-e",
        "GRANIAN_LOG_LEVEL=warning",
        "-e",
        "GRANIAN_LOG_ACCESS_ENABLED=false",
    ]
    docker_run(name, SEARXNG_IMAGE, options)
    try:
        wait_http(ports.url("searxng") + "/healthz", 60)
        yield
    finally:
        docker("stop", name)


@contextmanager
def stack(name: str = "atlas-chat", persistent: bool = True) -> Iterator[None]:
    ports = atlas_ports()
    name = ports.name(name)
    servers: list[uvicorn.Server] = []
    threads: list[threading.Thread] = []
    database = name + "-db"
    started = False
    gateway = None
    try:
        volume = (
            ["-v", ports.name("atlas-chat-index") + ":/var/lib/postgresql/data"]
            if persistent
            else []
        )
        docker_run(
            database,
            "pgvector/pgvector:pg16",
            [
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "-p",
                # A random loopback port: already safe for parallel stacks, and
                # 5432 is often taken (host Postgres, CI services).
                "127.0.0.1::5432",
                *volume,
            ],
        )
        started = True
        port = docker("port", database, "5432").rsplit(":", 1)[1]
        os.environ["ATLAS_RETRIEVAL_DSN"] = (
            f"host=127.0.0.1 port={port} user=postgres dbname=postgres connect_timeout=2"
        )
        for attempt in range(60):
            if (
                subprocess.run(
                    ["docker", "exec", database, "pg_isready", "-U", "postgres"],
                    capture_output=True,
                ).returncode
                == 0
            ):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("database_start_timeout")
        gateway = serve(CPUModels(), ports.port("gateway"))
        thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)
        os.environ["ATLAS_GATEWAY_URL"] = ports.url("gateway")
        store = Store(os.environ["ATLAS_RETRIEVAL_DSN"])
        store.initialize()
        ingest(Path("corpus"), store, Gateway(os.environ["ATLAS_GATEWAY_URL"]))
        for app, port_number in (
            ("services.retrieval.api:app", ports.port("retrieval")),
            ("services.orchestrator.chat_api:app", ports.port("adapter")),
        ):
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=port_number,
                    log_level="error",
                    access_log=False,
                )
            )
            servers.append(server)
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            threads.append(thread)
        wait_http(ports.url("retrieval") + "/openapi.json")
        wait_http(ports.url("adapter") + "/v1/models")
        if not all(server.started for server in servers):
            raise RuntimeError("service_bind_failed")
        yield
    finally:
        for server in servers:
            server.should_exit = True
        if gateway is not None:
            gateway.shutdown()
            gateway.server_close()
        for thread in threads:
            thread.join(timeout=10)
        if started:
            docker("stop", database)


def run() -> None:
    require_stable_configuration()
    require_env(CHAT_REQUIRED_ENV)
    os.environ.setdefault("ATLAS_SEARCH_PROVIDER", "tavily")
    # Scanned PDF pages are read by LightOn OCR (owner decision 2026-10-15).
    os.environ.setdefault("ATLAS_OCR_MODEL", "lightonocr-3-0.8b")
    providers = search_providers(os.environ["ATLAS_SEARCH_PROVIDER"])
    credentials = {"tavily": "TAVILY_API_KEY", "serpapi": "SERPAPI_KEY"}
    if not any(
        provider == "searxng" or os.environ.get(credentials[provider])
        for provider in providers
    ):
        require_env((credentials[providers[0]],) if providers[0] != "searxng" else ())
    os.environ["ATLAS_IMAGE_ON_DEMAND"] = "1"
    stopping = threading.Event()
    stack_failed = threading.Event()
    install_stop_handling(stopping, stack_failed)
    from services.orchestrator.terminal_stack import terminal, user_interface

    # Open Terminal owns only user files; its loopback publisher is not a host mount.
    ports = atlas_ports()
    # Loopback only, never a public interface; base 0 keeps the historical binding.
    binding = (
        "127.0.0.1:8000"
        if ports.base == 0
        else "127.0.0.1:" + str(ports.port("terminal"))
    )
    searxng_stack = searxng() if "searxng" in providers else nullcontext()
    try:
        with searxng_stack, terminal("atlas-open-terminal", binding), stack():
            os.environ["ATLAS_TERMINAL_ENABLED"] = "1"
            try:
                with user_interface("atlas-chat-ui", True):
                    if not stable_mode():
                        configure()
                    print(
                        "ATLAS ready: "
                        + ports.url("chat_ui", "localhost")
                        + " (SSH tunnel), GPU_LOCAL="
                        + os.environ.get("GPU_LOCAL", "0"),
                        flush=True,
                    )
                    while not stopping.wait(0.2):
                        pass
            finally:
                os.environ.pop("ATLAS_TERMINAL_ENABLED", None)
    except BaseException:
        if not stopping.is_set():
            stack_failed.set()
            log_json("stack_stopped", reason="stack_context_exception")
        raise
    require_accepted_stop(stopping, stack_failed)


def main() -> None:
    from services.orchestrator.serve_owner import ownership

    with ownership():
        run()


if __name__ == "__main__":
    main()
