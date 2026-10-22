"""Local user-file container with host-installed, fail-closed network containment."""

from contextlib import contextmanager
from collections.abc import Iterator
import json
import os
import socket
import hmac
import subprocess
import select
import socketserver
import threading
import re
import posixpath
from urllib.parse import unquote

import uvicorn
from packages.ports import atlas_ports
from services.orchestrator.ui_injection import inject_atlas_ui
from services.orchestrator.code_ui import legacy_project_route, project_mode
from services.retrieval.project_identity import ProjectError

NETWORK = "atlas-files"
IMAGE = "ghcr.io/open-webui/open-terminal@sha256:3d52176700f2442e20556ef129ae980613389be773e269cf7fa0118f3b55669d"


def disable_webui_file_processing(header: bytes) -> bytes:
    """Keep uploads as chat attachments; Atlas reads their originals itself.

    Open WebUI defaults both upload processing switches to true.  Changing only the
    request target lets the multipart stream pass through byte-for-byte, including
    large files, while preventing its separate extraction/indexing pipeline.
    """
    request_line, separator, remainder = header.partition(b"\r\n")
    fields = request_line.split(b" ", 2)
    if len(fields) != 3 or fields[0].upper() != b"POST":
        return header
    target = fields[1]
    path, query_marker, query = target.partition(b"?")
    if path.rstrip(b"/") != b"/api/v1/files":
        return header
    rewritten: list[bytes] = []
    seen = set[bytes]()
    for item in query.split(b"&") if query_marker else []:
        name = item.split(b"=", 1)[0]
        lowered = name.lower()
        if lowered in {b"process", b"process_in_background"}:
            rewritten.append(name + b"=false")
            seen.add(lowered)
        else:
            rewritten.append(item)
    for name in (b"process", b"process_in_background"):
        if name not in seen:
            rewritten.append(name + b"=false")
    fields[1] = path + b"?" + b"&".join(rewritten)
    return b" ".join(fields) + separator + remainder


def without_header(header: bytes, name: bytes) -> bytes:
    """Remove every case-insensitive HTTP header line with ``name``."""
    prefix = name.lower() + b":"
    return b"\r\n".join(
        line for line in header.split(b"\r\n") if not line.lower().startswith(prefix)
    )


def stable_route_allowed(header: bytes) -> bool:
    """Expose generated downloads, never the terminal's execution or browse API."""
    method, target, _ = header.split(b" ", 2)
    path = target.split(b"?", 1)[0].decode("latin1")
    if project_mode() and legacy_project_route(path):
        return False
    for _ in range(8):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    if unquote(path) != path or "\\" in path:
        return False
    path = posixpath.normpath(path)
    if path == "/api/v1/auths/signup":
        return False
    if path.startswith("/api/v1/terminals"):
        return method in (b"GET", b"HEAD") and (
            path == "/api/v1/terminals"
            or re.fullmatch(
                r"/api/v1/terminals/atlas-files/files/serve/home/user/requests/"
                r"chatcmpl-[a-f0-9]{32}/[A-Za-z0-9][A-Za-z0-9_.-]*",
                path,
            )
            is not None
        )
    return True


@contextmanager
def loopback_forward(
    binding: str, target: tuple[str, int]
) -> Iterator[tuple[str, int]]:
    """Publish an internal bridge API without giving it an outbound route."""
    host, port = binding.rsplit(":", 1)
    if host != "127.0.0.1":
        raise ValueError("terminal_requires_loopback")
    stopping = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            try:
                with socket.create_connection(target, timeout=5) as remote:
                    peers = {self.request: remote, remote: self.request}
                    while peers and not stopping.is_set():
                        readable, _, _ = select.select(list(peers), [], [], 0.2)
                        for source in readable:
                            data = source.recv(65536)
                            if data:
                                peers[source].sendall(data)
                            else:
                                peers.pop(source).shutdown(socket.SHUT_WR)
            except OSError:
                # Closing both sockets exposes a transport failure to the caller.
                self.request.close()

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    with Server((host, int(port)), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield str(server.server_address[0]), int(server.server_address[1])
        finally:
            stopping.set()
            server.shutdown()
            worker.join(timeout=5)


@contextmanager
def loopback_html_injector(
    binding: str, target: tuple[str, int]
) -> Iterator[tuple[str, int]]:
    """TCP relay that rewrites only the initial uncompressed HTML response.

    Upgrade connections and every non-HTML response are byte relays, preserving the
    WebUI API and websocket behaviour while avoiding a fork of its application.
    """
    from services.orchestrator.code_ui import relay_header
    from services.orchestrator.code_workspace import enabled as code_enabled

    host, port = binding.rsplit(":", 1)
    if host != "127.0.0.1":
        raise ValueError("ui_injector_requires_loopback")
    stable = os.environ.get("ATLAS_STABLE") == "1"
    proxy_secret = os.environ.get("ATLAS_PROXY_SECRET", "")
    if stable and not proxy_secret.strip():
        raise RuntimeError("missing_configuration: ATLAS_PROXY_SECRET")
    stopping = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            client = self.request
            try:
                with socket.create_connection(target, timeout=5) as remote:
                    remote.settimeout(None)
                    head = b""
                    while b"\r\n\r\n" not in head:
                        chunk = client.recv(65536)
                        if not chunk or len(head) > 1048576:
                            return
                        head += chunk
                    header, marker, body = head.partition(b"\r\n\r\n")
                    target_path = header.split(b" ", 2)[1].decode("latin1")
                    if project_mode() and legacy_project_route(target_path):
                        error = json.dumps(ProjectError(404, "not_found").body).encode()
                        client.sendall(
                            b"HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\n"
                            b"Cache-Control: private, no-store\r\nConnection: close\r\nContent-Length: "
                            + str(len(error)).encode()
                            + b"\r\n\r\n"
                            + error
                        )
                        return
                    if target_path.startswith("/api/atlas/") or target_path.startswith(
                        "/internal/atlas/"
                    ):
                        # The BFF verifies credentials and Origin against the original
                        # browser Host. No private credential reaches retrieval's public routes.
                        project_header = (
                            without_header(header, b"connection")
                            + b"\r\nConnection: close"
                        )
                        with socket.create_connection(
                            ("127.0.0.1", atlas_ports().port("adapter")), timeout=5
                        ) as adapter:
                            adapter.settimeout(None)
                            adapter.sendall(project_header + marker + body)
                            self.relay(client, adapter, header.split(b" ", 1)[0], False)
                        return
                    if header.split(b" ", 2)[1].startswith(b"/atlas-code/"):
                        forwarded = relay_header(header)
                        if forwarded is None:
                            client.sendall(
                                b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                            )
                            return
                        with socket.create_connection(
                            ("127.0.0.1", atlas_ports().port("adapter")), timeout=5
                        ) as adapter:
                            adapter.settimeout(None)
                            adapter.sendall(forwarded + marker + body)
                            self.relay(client, adapter, header.split(b" ", 1)[0], False)
                        return
                    if stable:
                        supplied_user = re.search(
                            rb"(?im)^x-atlas-user:[^\r\n]+", header
                        )
                        supplied_secret = re.search(
                            rb"(?im)^x-atlas-proxy-secret:\s*([^\r\n]*)", header
                        )
                        # The UI listener is loopback-only.  nginx, reached through
                        # the restricted SSH tunnel, is the only peer permitted to
                        # set this identity header.  Do not pass the shared secret
                        # beyond this relay, and replace any supplied role value.
                        if (
                            not stable_route_allowed(header)
                            or not supplied_user
                            or supplied_secret is None
                            or not hmac.compare_digest(
                                supplied_secret.group(1).strip().decode("latin1"),
                                proxy_secret,
                            )
                        ):
                            client.sendall(
                                b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                            )
                            return
                        header = without_header(header, b"x-atlas-proxy-secret")
                        header = without_header(header, b"x-atlas-user-role")
                        header += b"\r\nX-Atlas-User-Role: user"
                    header = disable_webui_file_processing(header)
                    method = header.split(b" ", 1)[0].upper()
                    upgrade = re.search(rb"(?im)^upgrade:", header) is not None
                    # Ask WebUI for an uncompressed shell so the injection can update
                    # its length. One request per connection, so every HTML response
                    # is seen, however the browser reuses connections.
                    header = re.sub(
                        rb"(?im)^accept-encoding:[^\r\n]*",
                        b"Accept-Encoding: identity",
                        header,
                    )
                    if not upgrade:
                        if re.search(rb"(?im)^connection:", header):
                            header = re.sub(
                                rb"(?im)^connection:[^\r\n]*",
                                b"Connection: close",
                                header,
                            )
                        else:
                            header += b"\r\nConnection: close"
                    remote.sendall(header + marker + body)
                    self.relay(client, remote, method, upgrade)
            except OSError:
                pass
            finally:
                try:
                    client.close()
                except OSError:
                    pass

        def relay(
            self,
            client: socket.socket,
            remote: socket.socket,
            method: bytes,
            upgrade: bool,
        ) -> None:
            """Stream both directions at once; rewrite only a GET 200 HTML response."""
            response = b""
            head = b""
            # -1: reading the response head, -2: plain relay, n >= 0: collecting body
            expected = -1
            peers = {client: remote, remote: client}
            while peers and not stopping.is_set():
                readable, _, _ = select.select(list(peers), [], [], 0.2)
                for source in readable:
                    data = source.recv(65536)
                    if source is client:
                        if data:
                            remote.sendall(data)
                        else:
                            peers.pop(client, None)
                            remote.shutdown(socket.SHUT_WR)
                        continue
                    if not data:
                        if response:
                            client.sendall(response)
                        peers.clear()
                        break
                    if expected == -2:
                        client.sendall(data)
                        continue
                    response += data
                    if expected == -1 and b"\r\n\r\n" in response:
                        head = response.partition(b"\r\n\r\n")[0]
                        length = re.search(rb"\r\ncontent-length: (\d+)", head, re.I)
                        status_ok = head.startswith((b"HTTP/1.1 200", b"HTTP/1.0 200"))
                        html = b"content-type: text/html" in head.lower()
                        if (
                            method == b"GET"
                            and not upgrade
                            and status_ok
                            and html
                            and length
                        ):
                            expected = int(length.group(1))
                        else:
                            client.sendall(response)
                            response, expected = b"", -2
                            continue
                    if expected >= 0:
                        body = response.partition(b"\r\n\r\n")[2]
                        if len(body) >= expected:
                            updated = inject_atlas_ui(
                                body[:expected], stable=stable, code_mode=code_enabled()
                            )
                            new_head = re.sub(
                                rb"(\r\ncontent-length: )\d+",
                                lambda m: m.group(1) + str(len(updated)).encode(),
                                head,
                                flags=re.I,
                            )
                            client.sendall(
                                new_head + b"\r\n\r\n" + updated + body[expected:]
                            )
                            response, expected = b"", -2

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    with Server((host, int(port)), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield str(server.server_address[0]), int(server.server_address[1])
        finally:
            stopping.set()
            server.shutdown()
            worker.join(timeout=5)


def command(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("terminal_setup_failed:" + args[0])
    return result.stdout.strip()


def network_gateway() -> str:
    network = atlas_ports().name(NETWORK)
    value = json.loads(command("docker", "network", "inspect", network))[0]
    if (
        not value.get("Internal")
        or value.get("Labels", {}).get("atlas.owner") != "files"
    ):
        raise RuntimeError("terminal_network_owner_mismatch")
    return str(value["IPAM"]["Config"][0]["Gateway"])


def firewall(pid: str, family: str, *args: str) -> str:
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    return command(*prefix, "nsenter", "-t", pid, "-n", family, *args)


def contain(name: str) -> None:
    pid = command("docker", "inspect", "--format", "{{.State.Pid}}", name)
    for family in ("iptables", "ip6tables"):
        # The API is held behind a startup latch until BOTH families are closed.
        # No new outbound connection is allowed, including Docker's DNS proxy.
        firewall(pid, family, "-P", "OUTPUT", "DROP")
        firewall(pid, family, "-F", "OUTPUT")
        firewall(
            pid,
            family,
            "-A",
            "OUTPUT",
            "-m",
            "conntrack",
            "--ctstate",
            "ESTABLISHED,RELATED",
            "-j",
            "ACCEPT",
        )
        firewall(pid, family, "-A", "OUTPUT", "-j", "REJECT")
        rules = firewall(pid, family, "-S", "OUTPUT")
        if "-P OUTPUT DROP" not in rules or "-A OUTPUT -j REJECT" not in rules:
            raise RuntimeError("terminal_containment_unverified")
    command("docker", "exec", name, "touch", "/tmp/atlas-network-ready")


def adapter_socket(port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((network_gateway(), port))
    listener.listen(128)
    listener.setblocking(False)
    return listener


@contextmanager
def terminal(name: str, binding: str) -> Iterator[None]:
    from services.orchestrator.serving import wait_http

    ports = atlas_ports()
    network = ports.name(NETWORK)
    name = ports.name(name)
    if not os.environ.get("OPEN_TERMINAL_API_KEY", "").strip():
        raise RuntimeError("missing_configuration: OPEN_TERMINAL_API_KEY")
    exists = command(
        "docker",
        "network",
        "ls",
        "--filter",
        "name=^" + network + "$",
        "--format",
        "{{.Name}}",
    )
    if not exists:
        command(
            "docker",
            "network",
            "create",
            "--internal",
            "--label",
            "atlas.owner=files",
            network,
        )
    network_gateway()
    existing = command(
        "docker",
        "ps",
        "-a",
        "--filter",
        "name=^/" + name + "$",
        "--format",
        "{{.Names}}",
    )
    if existing:
        details = json.loads(command("docker", "inspect", name))[0]
        if details["Config"].get("Labels", {}).get("atlas.owner") != "files":
            raise RuntimeError("terminal_container_owner_mismatch")
        command("docker", "rm", "-f", name)
    from services.orchestrator.code_workspace import enabled, volume

    code_mount = (
        ["--mount", f"type=volume,src={volume()},dst=/code-workspaces,readonly"]
        if enabled()
        else []
    )
    started = False
    try:
        command(
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "atlas.owner=files",
            "--network",
            network,
            "--network-alias",
            "open-terminal",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--read-only",
            "--memory",
            "2g",
            "--cpus",
            "2",
            "--pids-limit",
            "128",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=128m,uid=1000,gid=1000",
            "-v",
            ports.name("atlas-terminal-files") + ":/home/user",
            "-e",
            "OPEN_TERMINAL_API_KEY",
            "-e",
            "OPEN_TERMINAL_EXECUTE_TIMEOUT=30",
            *code_mount,
            "--entrypoint",
            "/bin/sh",
            IMAGE,
            "-c",
            "while ! test -f /tmp/atlas-network-ready; do sleep 0.1; done; "
            "exec python3 -m open_terminal run --host 0.0.0.0 --port "
            + str(ports.port("terminal")),
        )
        started = True
        contain(name)
        details = json.loads(command("docker", "inspect", name))[0]
        address = details["NetworkSettings"]["Networks"][network]["IPAddress"]
        with loopback_forward(binding, (address, ports.port("terminal"))):
            try:
                wait_http("http://" + binding + "/docs", 30)
            except RuntimeError:
                logs = subprocess.run(
                    ["docker", "logs", name], capture_output=True, text=True
                )
                detail = (logs.stdout + logs.stderr).replace(
                    os.environ["OPEN_TERMINAL_API_KEY"], "[REDACTED]"
                )
                raise RuntimeError("terminal_start_failed: " + detail[-2000:]) from None
            yield
    finally:
        if started and command(
            "docker",
            "ps",
            "-a",
            "--filter",
            "name=^/" + name + "$",
            "--format",
            "{{.Names}}",
        ):
            command("docker", "stop", name)
            command("docker", "rm", name)


@contextmanager
def user_interface(name: str, persistent: bool) -> Iterator[None]:
    """Expose the native UI and adapter on the contained user-file network."""
    from services.orchestrator.serving import (
        docker,
        stt_enabled,
        wait_http,
        webui,
        stable_mode,
    )
    from services.orchestrator.webui import configure

    ports = atlas_ports()
    network = ports.name(NETWORK)
    container = ports.name(name)
    listener = adapter_socket(ports.port("adapter"))
    adapter = uvicorn.Server(
        uvicorn.Config(
            "services.orchestrator.chat_api:app", log_level="error", access_log=False
        )
    )
    worker = threading.Thread(
        target=lambda: adapter.run(sockets=[listener]), daemon=True
    )
    worker.start()
    tts_listener = None
    tts = None
    tts_worker = None
    if os.environ.get("ATLAS_TTS") == "1":
        tts_listener = adapter_socket(ports.port("tts"))
        tts = uvicorn.Server(
            uvicorn.Config(
                "services.tts.service:app", log_level="error", access_log=False
            )
        )
        tts_worker = threading.Thread(
            target=lambda: tts.run(sockets=[tts_listener]), daemon=True
        )
        tts_worker.start()
    stt_listener = None
    stt = None
    stt_worker = None
    if stt_enabled():
        stt_listener = adapter_socket(ports.port("stt"))
        stt = uvicorn.Server(
            uvicorn.Config(
                "services.stt.service:app", log_level="error", access_log=False
            )
        )
        stt_worker = threading.Thread(
            target=lambda: stt.run(sockets=[stt_listener]), daemon=True
        )
        stt_worker.start()
    previous_host_url = os.environ.get("ATLAS_WEBUI_URL")
    started = False
    try:
        webui(name, persistent)
        started = True
        details = json.loads(docker("inspect", container))[0]
        address = details["NetworkSettings"]["Networks"][network]["IPAddress"]
        os.environ.setdefault(
            "ATLAS_WEBUI_URL", f"http://{address}:{ports.port('chat_ui')}"
        )
        binding = "127.0.0.1:" + str(ports.port("chat_ui"))
        if stable_mode():
            endpoint = f"http://{address}:{ports.port('chat_ui')}"
            wait_http(endpoint)
            # Provision only over the private container network, before opening
            # the relay. Guest requests always receive the ordinary user role.
            configure(
                endpoint,
                {
                    "X-Atlas-User": "atlas-bootstrap@localhost",
                    "X-Atlas-User-Role": "admin",
                },
            )
        with loopback_html_injector(binding, (address, ports.port("chat_ui"))):
            if not stable_mode():
                wait_http(ports.url("chat_ui"))
            yield
    finally:
        if previous_host_url is None:
            os.environ.pop("ATLAS_WEBUI_URL", None)
        if started:
            docker("stop", container)
        adapter.should_exit = True
        worker.join(timeout=10)
        listener.close()
        if tts is not None and tts_worker is not None and tts_listener is not None:
            tts.should_exit = True
            tts_worker.join(timeout=10)
            tts_listener.close()
        if stt is not None and stt_worker is not None and stt_listener is not None:
            stt.should_exit = True
            stt_worker.join(timeout=10)
            stt_listener.close()
