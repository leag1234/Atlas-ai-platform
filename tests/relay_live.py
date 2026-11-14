"""Opt-in live proof: Atlas Work on the user's own computer through the Atlas relay.

Real pieces: tenant database, retrieval, runner broker, model gateway with a
live provider, the Work tools and agent loop, the relay image (infra/relay),
the connection script (services/runner/connect.sh) with the pinned upterm
release. Simulated: the user's computer is a container that accepts no inbound
connection (iptables INPUT DROP), the server of his local network a second
container reachable only from it; the conversation is replayed turn by turn,
and its yes/no answers are read back by the same code as behind the chat.

Run (costs live model calls, bounded by RELAY_LIVE_EUR, default 1.2):
  PYTHONPATH=.:tests:services/model-gateway:services/edge-bff python3 tests/relay_live.py
Prints the turns and a JSON summary; never prints a key or a token.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
from typing import Any
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "remotehost-live-"
COMPUTER, LAN, RELAY = PREFIX + "computer", PREFIX + "buildbox", PREFIX + "relay"
WAN, HOME_LAN = PREFIX + "wan", PREFIX + "lan"


def docker(*args: str, check: bool = True, data: str | None = None) -> str:
    reply = subprocess.run(
        ["docker", *args], capture_output=True, text=True, input=data, timeout=600
    )
    if check and reply.returncode:
        raise RuntimeError("docker " + args[0] + ": " + reply.stderr[-400:])
    return reply.stdout.strip()


def shell(container: str, script: str, user: str = "ada", check: bool = True) -> str:
    return docker(
        "exec", "-i", "-u", user, container, "sh", "-s", check=check, data=script
    )


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def cleanup() -> None:
    for name in (COMPUTER, LAN, RELAY):
        docker("rm", "-f", name, check=False)
    for name in (WAN, HOME_LAN):
        docker("network", "rm", name, check=False)


def environment(state: Path) -> dict[str, str]:
    """Containers, networks and the project on the user's computer."""
    docker("build", "-q", "-t", PREFIX + "relay:local", str(ROOT / "infra/relay"))
    docker(
        "build",
        "-q",
        "-t",
        PREFIX + "computer:local",
        "-f",
        str(ROOT / "tests/relay/Dockerfile.computer"),
        str(ROOT / "tests/relay"),
    )
    docker(
        "build",
        "-q",
        "-t",
        PREFIX + "lan:local",
        "-f",
        str(ROOT / "tests/relay/Dockerfile.lan"),
        str(ROOT / "tests/relay"),
    )
    docker("network", "create", WAN)
    docker("network", "create", "--internal", HOME_LAN)
    gate = state / "gate"
    keys = state / "relay-keys"
    gate.mkdir(mode=0o755)
    keys.mkdir()
    os.chown(keys, 1002, 1002)
    docker(
        "run",
        "-d",
        "--name",
        RELAY,
        "--network",
        WAN,
        "-p",
        "127.0.0.1::2222",
        "-v",
        f"{gate}:/gate:ro",
        "-v",
        f"{keys}:/keys",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--cap-drop",
        "ALL",
        PREFIX + "relay:local",
    )
    docker(
        "run",
        "-d",
        "--name",
        LAN,
        "--network",
        HOME_LAN,
        "--hostname",
        "buildbox",
        PREFIX + "lan:local",
    )
    docker(
        "run",
        "-d",
        "--name",
        COMPUTER,
        "--network",
        WAN,
        "--hostname",
        "ada-laptop",
        "--cap-add",
        "NET_ADMIN",
        PREFIX + "computer:local",
        "sleep",
        "infinity",
    )
    docker("network", "connect", HOME_LAN, COMPUTER)
    # The user's computer accepts no inbound connection at all.
    shell(
        COMPUTER,
        "iptables -P INPUT DROP && iptables -A INPUT -i lo -j ACCEPT && iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        user="root",
    )
    # His own key on his server, his own ssh config, a bare repository there.
    public = shell(
        COMPUTER,
        "mkdir -p ~/.ssh && ssh-keygen -q -t ed25519 -N '' -f ~/.ssh/id_ed25519 && printf 'Host buildbox\\n  HostName %s\\n  User ada\\n  StrictHostKeyChecking accept-new\\n' "
        + LAN
        + " > ~/.ssh/config && cat ~/.ssh/id_ed25519.pub",
    )
    docker(
        "exec",
        "-i",
        LAN,
        "sh",
        "-c",
        "cat > /home/ada/.ssh/authorized_keys && chown -R ada:ada /home/ada/.ssh && chmod 600 /home/ada/.ssh/authorized_keys && su ada -c 'git init -q --bare /home/ada/orbit.git'",
        data=public + "\n",
    )
    shell(
        COMPUTER,
        """set -e
mkdir -p ~/orbit && cd ~/orbit
git init -q -b main && git config user.name 'Ada Example' && git config user.email ada@example.org
cat > orbit.py <<'PY'
def period(radius_km, mass_kg):
    import math
    g = 6.674e-11
    r = radius_km * 1000
    return 2 * math.pi * math.sqrt(r ** 3 / (g * mass_kg))
PY
cat > test_orbit.py <<'PY'
import unittest
from orbit import period


class PeriodTests(unittest.TestCase):
    def test_low_earth_orbit_is_about_ninety_minutes(self):
        self.assertAlmostEqual(period(6771, 5.972e24) / 60, 92.4, delta=1)


if __name__ == "__main__":
    unittest.main()
PY
printf 'build:\\n\\tpython3 -m py_compile orbit.py\\n\\ntest: build\\n\\tpython3 -m unittest -v\\n' > Makefile
git add -A && git commit -qm 'Orbital period' && git remote add origin buildbox:orbit.git
""",
    )
    key = ""
    for _ in range(30):
        key = docker("exec", RELAY, "cat", "/keys/relay_ed25519.pub", check=False)
        if key:
            break
        time.sleep(1)
    port = docker("port", RELAY, "2222").splitlines()[0].rsplit(":", 1)[1]
    gateway = docker(
        "network", "inspect", WAN, "--format", "{{(index .IPAM.Config 0).Gateway}}"
    )
    return dict(
        ATLAS_RELAY_PUBLIC_URL=f"ws://{RELAY}:8080/",
        ATLAS_RELAY_SSH="127.0.0.1:" + port,
        ATLAS_RELAY_HOST_KEY=" ".join(key.split()[:2]),
        ATLAS_RELAY_GATE=str(gate / "hosts"),
        ATLAS_RELAY_CONNECT_ORIGIN=f"http://{gateway}:{free_port()}",
        ATLAS_REMOTE_RUNNERS="1",
        ATLAS_CODE_MODE="1",
        ATLAS_PROJECT_IDENTITY_SECRET=os.urandom(32).hex(),
    )


def main() -> None:
    import tempfile

    import uvicorn
    from fastapi import FastAPI

    import test_runner_registry as registry
    from gateway_cpu import CPUModels
    from http_gateway import serve
    from packages.language import language_instruction
    from packages.ports import atlas_ports
    from services.orchestrator import machine_connect
    from services.orchestrator import work_machine_tools as machines
    from services.orchestrator.code_mode import CodeModel, WorkExecution, code_profile
    from services.orchestrator.code_tools import WorkTools, declarations
    from services.orchestrator.code_workspace import Workspace
    from services.orchestrator.loop import Limits, Query, run
    from services.orchestrator.work_tool_selection import WorkToolSelection

    budget = Decimal(os.environ.get("RELAY_LIVE_EUR", "1.2"))
    cleanup()
    state = Path(tempfile.mkdtemp(dir=ROOT / "BRAIN", prefix="relay-live-"))
    summary: dict[str, Any] = dict(turns=[])

    class Fixture(registry.RunnerRegistryTests):
        def runTest(self) -> None:  # pragma: no cover - fixture only
            pass

    Fixture.setUpClass()
    fixture = Fixture()
    servers: list[Any] = []
    try:
        fixture.setUp()
        settings = environment(state)
        variables = patch.dict(os.environ, settings)
        variables.start()
        fixture.addCleanup(variables.stop)
        # Gateway (live provider) and the connection routes, as the instance serves them.
        port = atlas_ports().port("gateway")
        gateway = serve(CPUModels(), port)
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        servers.append(gateway)
        os.environ["ATLAS_GATEWAY_URL"] = f"http://127.0.0.1:{port}"

        async def as_member(
            claims: dict[str, Any], tail: str, data: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            # The orchestrator resolves the token's member and calls his own
            # machines API; here that API is the fixture's, as that member.
            assert claims["o"] == fixture.org
            reply = fixture.request(
                "POST",
                f"/api/atlas/orgs/{fixture.org}/machines/{claims['m']}/{tail}",
                "editor",
                data,
            )
            if reply.status_code != 200:
                print(
                    "connect route",
                    tail,
                    reply.status_code,
                    reply.text[:300],
                    flush=True,
                )
            return reply.status_code, reply.json()

        member = patch.object(machine_connect, "_as_member", as_member)
        member.start()
        fixture.addCleanup(member.stop)
        app = FastAPI()
        app.include_router(machine_connect.router)
        origin = settings["ATLAS_RELAY_CONNECT_ORIGIN"].rsplit(":", 1)
        config = uvicorn.Config(
            app, host=origin[0].split("//")[1], port=int(origin[1]), log_level="warning"
        )
        web = uvicorn.Server(config)
        threading.Thread(target=web.run, daemon=True).start()
        servers.append(web)
        time.sleep(1)

        class Broker:
            subject, org, project = "ada@example.org", fixture.org, fixture.project

            async def call(
                self, operation: str, data: dict[str, Any]
            ) -> dict[str, Any]:
                response = fixture.broker(operation, data)
                if response.status_code != 200:
                    from services.retrieval.project_identity import ProjectError

                    error = ProjectError(
                        response.status_code, str(response.json().get("error"))
                    )
                    error.body = response.json()
                    raise error
                return dict(response.json())

            async def member(
                self,
                method: str,
                tail: str,
                data: Any = None,
                revision: Any = None,
                timeout: float = 10,
            ) -> tuple[int, dict[str, Any]]:
                reply = fixture.request(
                    method,
                    f"/api/atlas/orgs/{fixture.org}/{tail}",
                    "editor",
                    data,
                    match=revision if isinstance(revision, int) else None,
                )
                return reply.status_code, reply.json()

            async def public(
                self, method: str, tail: str, body: bytes = b"", headers: Any = None
            ) -> tuple[int, dict[str, Any]]:
                reply = fixture.request(
                    method,
                    fixture.project_path() + "/" + tail,
                    "editor",
                    json.loads(body) if body else None,
                )
                return reply.status_code, reply.json()

        class Scratch(Workspace):
            def metadata(self) -> dict[str, Any]:
                return dict(baseline={})

        def current_session() -> dict[str, Any]:
            return dict(
                fixture.request(
                    "GET",
                    fixture.project_path() + "/work-sessions/" + fixture.session["id"],
                    "editor",
                ).json()
            )

        session = current_session()
        history: list[dict[str, Any]] = []
        spent = Decimal(0)
        turns_started = 0

        def new_turn() -> None:
            """End the Work turn and start the next one with its own id, as the
            chat does: the release invalidates the approvals left open."""
            nonlocal session
            released = fixture.turn(
                "release", terminal_state="completed", cleanup_confirmed=True
            )
            if released.status_code != 200:
                raise RuntimeError("release refused: " + released.text[:200])
            generation = fixture.fence()["generation"] + 1
            fixture.session = current_session()
            fixture.turn_id = str(uuid4())
            fixture.fence = lambda: dict(  # type: ignore[method-assign]
                session_id=fixture.session["id"],
                request_id=fixture.turn_id,
                generation=generation,
            )
            acquired = fixture.acquire()
            if acquired.status_code != 200:
                raise RuntimeError("acquire refused: " + acquired.text[:200])
            session = current_session()

        async def turn(text: str, approval: dict[str, Any] | None = None) -> str:
            nonlocal spent, turns_started
            if turns_started:
                new_turn()
            turns_started += 1
            tools = WorkTools(
                Scratch(str(uuid4())), WorkExecution(Broker(), session, fixture.turn_id)
            )
            tools.selection = WorkToolSelection("remote")
            tools.approvals_unattended = True
            tools.lang = "en"
            tools.machine_approval = approval
            tools.instructions = machines.instructions(True)
            model = await CodeModel.connect(
                os.environ["ATLAS_GATEWAY_URL"], local_enabled=False
            )
            assert isinstance(model, CodeModel)
            model.configure_quality(code_profile(), 2048)
            model.tools = declarations()
            # As code_mode does: the model sees the selected group's tools.
            model.work_tools = tools.selection.declarations
            model.work_cost = Decimal("0.5")
            model.sink = None
            system = (
                (ROOT / "prompts/chat.txt").read_text()
                + (ROOT / "prompts/chat-agent.txt").read_text()
                + language_instruction("en")
                + tools.instructions
            )
            calls: list[str] = []
            original = tools.execute

            async def recorded(call: Any, timeout: float) -> Any:
                result = await original(call, timeout)
                shown = {
                    k: result.get(k)
                    for k in ("state", "exit_code", "error", "output", "message")
                    if isinstance(result, dict) and result.get(k) is not None
                }
                shown = json.loads(
                    re.sub(
                        r"[A-Za-z0-9_-]{60,}\.[A-Za-z0-9_-]{30,}",
                        "<token>",
                        json.dumps(shown),
                    )
                )
                arguments = re.sub(
                    r"[A-Za-z0-9_-]{60,}\.[A-Za-z0-9_-]{30,}", "<token>", call.arguments
                )
                calls.append(
                    f"{call.name} {arguments[:300]} -> {json.dumps(shown)[:600]}"
                )
                return result

            tools.execute = recorded  # type: ignore[method-assign]
            result = await run(
                Query(
                    question=text + (machine_hint(approval) if approval else ""),
                    lang="en",
                ),
                model,
                tools,
                system,
                Limits(
                    profile=code_profile(),
                    tokens=None,
                    code_mode=True,
                    tool_calls=30,
                    wall_clock=600,
                    cost=Decimal("0.5"),
                    model_calls=30,
                ),
                history=list(history),
                final_only=True,
            )
            answer = machines.complete_answer(result.text, tools.machine_commands, "en")
            answer = machines.complete_approvals(answer, tools.machine_approvals)
            spent += model.spent
            history.extend(
                [
                    dict(role="user", content=text),
                    dict(role="assistant", content=answer),
                ]
            )
            shown_answer = re.sub(
                r"[A-Za-z0-9_-]{60,}\.[A-Za-z0-9_-]{30,}", "<token>", answer
            )
            print(
                f"\n=== USER: {text}\n--- tools:\n"
                + "\n".join(calls)
                + f"\n--- ATLAS:\n{shown_answer}\n--- cost so far {spent:.4f} EUR",
                flush=True,
            )
            summary["turns"].append(
                dict(user=text, tools=calls, cost_eur=str(model.spent))
            )
            if spent > budget:
                raise RuntimeError("live budget reached")
            return answer

        def machine_hint(approval: dict[str, Any]) -> str:
            from services.orchestrator.chat_work import machine_approval_hint

            return machine_approval_hint(approval)

        def reply_to(answer: str, text: str) -> dict[str, Any] | None:
            """What chat_work does with the person's next message."""
            asked = machines.approval_shown(answer)
            allowed = machines.consent(text) if asked else None
            if asked is None or allowed is None:
                return None
            return dict(asked, allowed=allowed)

        # A Work turn keeps its lease alive while it runs (30 s otherwise).
        renewing = threading.Event()

        def renew() -> None:
            while not renewing.wait(10):
                fixture.turn("renew")

        threading.Thread(target=renew, daemon=True).start()
        fixture.addCleanup(renewing.set)
        loop = asyncio.new_event_loop()
        if os.environ.get("RELAY_LIVE_DRY") == "1":
            # The same tools called directly, without a model: plumbing only.
            from services.orchestrator.loop import Call

            def tool(
                tool_name: str, approval: dict[str, Any] | None = None, **arguments: Any
            ) -> Any:
                tools = WorkTools(
                    Scratch(str(uuid4())),
                    WorkExecution(Broker(), session, fixture.turn_id),
                )
                tools.selection = WorkToolSelection("remote")
                tools.approvals_unattended, tools.lang = True, "en"
                tools.machine_approval = approval
                result = loop.run_until_complete(
                    tools.execute(
                        Call(str(uuid4()), tool_name, json.dumps(arguments)), 120
                    )
                )
                text = re.sub(
                    r"[A-Za-z0-9_-]{60,}\.[A-Za-z0-9_-]{30,}",
                    "<token>",
                    json.dumps(result)[:900],
                )
                print(
                    f"--- {tool_name} {json.dumps(arguments)[:200]}\n{text}", flush=True
                )
                return result

            connected = tool("machine_connect", name="ada-laptop")
            docker(
                "exec",
                "-d",
                "-u",
                "ada",
                "-w",
                "/home/ada/orbit",
                COMPUTER,
                "setsid",
                "sh",
                "-c",
                "echo $$ > /home/ada/connect.pid; "
                + connected["connect_command"]
                + " > /home/ada/connect.log 2>&1",
            )
            for _ in range(90):
                log = shell(COMPUTER, "cat ~/connect.log 2>/dev/null || true")
                if "Atlas is connected" in log or "Atlas:" in log:
                    break
                time.sleep(2)
            print("=== terminal\n" + log, flush=True)
            print(
                "relay:",
                docker("logs", "--tail", "5", RELAY, check=False),
                docker(
                    "ps", "-a", "--filter", "name=" + RELAY, "--format", "{{.Status}}"
                ),
                flush=True,
            )
            tool("machine_verify", name="ada-laptop")
            tool(
                "remote_write_file",
                target="ada-laptop",
                path="notes/hello.txt",
                content="hello\n",
            )
            tool("remote_run", target="ada-laptop", command="make test", wait_s=20)
            tool(
                "remote_run",
                target="ada-laptop",
                command="ssh buildbox uname -a",
                wait_s=20,
            )
            asked = tool(
                "remote_run",
                target="ada-laptop",
                command="git push origin main",
                wait_s=20,
            )
            answer = machines.approval_shown(asked.get("approval_question", "")) or {}
            # The yes comes in the next turn, after the release of this one.
            new_turn()
            tool(
                "remote_run",
                dict(answer, allowed=True),
                target="ada-laptop",
                command="git push origin main",
                wait_s=20,
            )
            print(
                shell(COMPUTER, "ls -A ~/orbit; cat ~/orbit/notes/hello.txt"),
                flush=True,
            )
            print(
                docker(
                    "exec",
                    "-u",
                    "ada",
                    LAN,
                    "git",
                    "-C",
                    "/home/ada/orbit.git",
                    "log",
                    "--oneline",
                    "-2",
                    check=False,
                ),
                flush=True,
            )
            print(
                shell(
                    COMPUTER,
                    "/bin/kill -INT -- -$(cat ~/connect.pid); sleep 8; tail -n 3 ~/connect.log",
                ),
                flush=True,
            )
            tool("machine_verify", name="ada-laptop")
            return
        first = loop.run_until_complete(
            turn(
                "I'm on my laptop, in my project folder orbit. Add a function circular_velocity(radius_km, mass_kg) to orbit.py with a unit test, then build, run the tests and commit."
            )
        )
        command = re.search(r"```bash\n(mkdir -p ~/\.atlas [^\n]+)\n```", first)
        summary["connect_blocks_in_answer"] = first.count("~/.atlas/connect.sh http")
        if command is None:
            raise RuntimeError("no connection command in the answer")
        # The user pastes the command in a terminal of his computer, in the project.
        docker(
            "exec",
            "-d",
            "-u",
            "ada",
            "-w",
            "/home/ada/orbit",
            COMPUTER,
            "setsid",
            "sh",
            "-c",
            "echo $$ > /home/ada/connect.pid; "
            + command[1]
            + " > /home/ada/connect.log 2>&1",
        )
        for _ in range(90):
            log = shell(COMPUTER, "cat ~/connect.log 2>/dev/null || true")
            if "Atlas is connected" in log or "Atlas:" in log:
                break
            time.sleep(2)
        print("\n=== the user's terminal:\n" + log, flush=True)
        # Listening sockets on the computer (hex local address): only Docker's
        # embedded DNS resolver (127.0.0.11) is expected; upterm listens nowhere.
        summary["listening_on_computer"] = shell(
            COMPUTER,
            "python3 -c \"import socket,struct;print(sorted({socket.inet_ntoa(struct.pack('<I',int(l.split()[1].split(':')[0],16)))+':'+str(int(l.split()[1].split(':')[1],16)) for l in open('/proc/net/tcp').read().splitlines()[1:] if l.split()[3]=='0A'}))\"",
        )
        summary["inbound_policy"] = shell(
            COMPUTER, "iptables -S INPUT | head -1", user="root"
        )
        loop.run_until_complete(turn("Done, it says connected."))
        loop.run_until_complete(
            turn(
                "Thanks. Now on buildbox, the server of my local network, show the kernel version and the free disk space."
            )
        )
        fourth = loop.run_until_complete(turn("Push the commit to origin."))
        approval = reply_to(fourth, "yes")
        summary["approval_asked"] = machines.approval_shown(fourth)
        if approval is not None:
            loop.run_until_complete(turn("yes", approval))
        summary["distinct_turn_ids"] = turns_started
        summary["git_log_on_computer"] = shell(
            COMPUTER, "cd ~/orbit && git log --oneline -3 && git status --short | head"
        )
        summary["pushed_on_buildbox"] = shell(
            LAN,
            "cd /home/ada/orbit.git && git log --oneline -3 main 2>&1",
            check=False,
        )
        summary["records_in_project"] = shell(COMPUTER, "ls -A ~/orbit")
        summary["cost_eur"] = str(spent)
        # The user ends the access: Ctrl-C (SIGINT) to the script, then Atlas cannot reach it.
        summary["terminal_after_ctrl_c"] = shell(
            COMPUTER,
            "/bin/kill -INT -- -$(cat ~/connect.pid); sleep 8; tail -n 3 ~/connect.log",
        )
        targets = fixture.request(
            "GET", f"/api/atlas/orgs/{fixture.org}/machines", "editor"
        ).json()["items"]
        summary["after_ctrl_c"] = [
            dict(name=t["name"], connected=t.get("connected"), status=t["status"])
            for t in targets
        ]
        print("\n=== SUMMARY\n" + json.dumps(summary, indent=2), flush=True)
        (state / "summary.json").write_text(json.dumps(summary, indent=2))
    finally:
        for server in servers:
            if hasattr(server, "should_exit"):
                server.should_exit = True
            else:
                server.shutdown()
                server.server_close()
        fixture.doCleanups()
        Fixture.doClassCleanups()
        cleanup()


if __name__ == "__main__":
    main()
