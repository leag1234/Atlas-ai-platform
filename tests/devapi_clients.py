"""Run actual pinned coding clients in containers exposing only this API socket."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from time import monotonic, sleep
from typing import Any
from services.orchestrator.dev_auth import Store, provision

IMAGE = "python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
LIMITS = [
    "--network",
    "none",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "--memory",
    "1g",
    "--cpus",
    "1",
    "--pids-limit",
    "128",
]


def command(client: str) -> list[str]:
    task = Path("tests/journeys/dev-client.txt").read_text().strip()
    if client == "claude":
        return [
            "/opt/client",
            "--bare",
            "--model",
            "atlas-code",
            "--tools",
            "Bash",
            "--allowedTools",
            "Bash",
            "--system-prompt",
            Path("tests/journeys/dev-client-system.txt").read_text(),
            "-p",
            task,
        ]
    settings: dict[str, Any] = {
        "model_provider": "atlas",
        "model_providers.atlas.name": "Atlas",
        "model_providers.atlas.base_url": "http://127.0.0.1:18030/v1",
        "model_providers.atlas.env_key": "ATLAS_API_KEY",
        "model_providers.atlas.wire_api": "responses",
        "features.enable_request_compression": False,
        "features.multi_agent": False,
        "features.goals": False,
        "features.remote_plugin": False,
        "model_instructions_file": "/work/instructions.md",
        "model_reasoning_summary": "none",
        "model_reasoning_effort": "none",
        "web_search": "disabled",
    }
    args = [
        "/opt/client/bin/codex",
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-m",
        "atlas-code",
    ]
    for key, value in settings.items():
        args += ["-c", key + "=" + json.dumps(value)]
    return args + [task]


def run_client(client: str, root: Path, sockets: Path, store: Store) -> dict[str, Any]:
    binary = Path(
        os.environ[
            "ATLAS_CODEX_VENDOR_DIR" if client == "codex" else "ATLAS_CLAUDE_BINARY"
        ]
    ).resolve(strict=True)
    version = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            *LIMITS,
            "-v",
            str(binary) + ":/opt/client:ro",
            IMAGE,
            "/opt/client/bin/codex" if client == "codex" else "/opt/client",
            "--version",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    expected = "codex-cli 0.153.4" if client == "codex" else "2.1.268 (Claude Code)"
    assert version.returncode == 0 and version.stdout.strip() == expected, (
        "client version differs"
    )
    work = root / (client + "-work")
    work.mkdir()
    shutil.copyfile("tests/journeys/dev-client-system.txt", work / "instructions.md")
    keyfile = root / (client + ".key")
    provision(store, client, keyfile, 4, 50000)
    key = keyfile.read_text().strip()
    envfile = root / (client + ".env")
    envfile.touch(mode=0o600)
    envfile.write_text(
        ("ATLAS_API_KEY" if client == "codex" else "ANTHROPIC_API_KEY")
        + "="
        + key
        + "\n"
    )
    name = "atlas-m15-" + client + "-" + root.name
    args = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        *LIMITS,
        "-v",
        str(binary) + ":/opt/client:ro",
        "-v",
        str(sockets) + ":/gateway:ro",
        "-v",
        str(Path("tests/devapi_relay.py").resolve()) + ":/opt/relay.py:ro",
        "-v",
        str(work) + ":/work",
        "-w",
        "/work",
        "--env-file",
        str(envfile),
    ]
    if client == "claude":
        for setting in [
            "ANTHROPIC_BASE_URL=http://127.0.0.1:18030",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
            "CLAUDE_CODE_ATTRIBUTION_HEADER=0",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS=512",
            "CLAUDE_CODE_DISABLE_THINKING=1",
        ]:
            args += ["-e", setting]
    args += [IMAGE, "python", "/opt/relay.py", *command(client)]
    output = b""
    try:
        process = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180
        )
        code, output = process.returncode, process.stdout
    except subprocess.TimeoutExpired as exc:
        code, output = 124, exc.stdout or b""
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    text = output.decode(errors="replace")
    for secret in [key] + [
        os.environ[k]
        for k in ("SCW_GENERATIVE_API_KEY", "GITHUB_TOKEN")
        if os.environ.get(k)
    ]:
        text = text.replace(secret, "[REDACTED]")
    log = Path("BRAIN/eval") / ("devapi-client-" + client + ".log")
    log.touch(mode=0o600)
    log.write_text(text)
    proof: dict[str, Any] = {
        "client": client,
        "version": expected,
        "exit_code": code,
        "usage": store.usage(client),
    }
    path = work / "clean_text.py"
    if code == 0 and path.is_file() and not path.is_symlink():
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                *LIMITS,
                "-v",
                str(work) + ":/work:ro",
                "-v",
                str(Path("tests/devapi_grader.py").resolve()) + ":/grader.py:ro",
                IMAGE,
                "python",
                "/grader.py",
            ],
            capture_output=True,
            timeout=30,
        )
        proof["grader_exit_code"] = result.returncode
        if result.returncode == 0:
            proof.update(json.loads(result.stdout))
    return proof


def main() -> None:
    report = Path("BRAIN/eval/devapi-clients.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    proofs = []
    with tempfile.TemporaryDirectory(prefix="atlas-m15-") as directory:
        root = Path(directory)
        sockets = root / "socket"
        sockets.mkdir(mode=0o700)
        store = Store(root / "usage.sqlite")
        env = {
            k: os.environ[k]
            for k in (
                "PATH",
                "SCW_GENERATIVE_BASE_URL",
                "SCW_GENERATIVE_API_KEY",
                "CODE_MODEL",
            )
            if k in os.environ
        }
        env.update(
            PYTHONPATH=".:services/model-gateway", ATLAS_DEVAPI_DB=str(store.path)
        )
        with (root / "api.log").open("w") as log:
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "services.orchestrator.devapi:app",
                    "--uds",
                    str(sockets / "api.sock"),
                    "--no-access-log",
                ],
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                deadline = monotonic() + 10
                while not (sockets / "api.sock").is_socket():
                    assert server.poll() is None and monotonic() < deadline, (
                        "API startup failed"
                    )
                    sleep(0.05)
                for client in ("codex", "claude"):
                    proof = run_client(client, root, sockets, store)
                    proofs.append(proof)
                    report.write_text(json.dumps(proofs, indent=2) + "\n")
                    assert 0 < proof["usage"]["charged_micro_eur"] <= 50000
                    assert proof["usage"]["unknown_micro_eur"] == 0
                    assert (
                        proof.get("grader_exit_code") == 0 and proof.get("checks") == 4
                    ), client + " failed"
            finally:
                server.terminate()
                server.wait(timeout=10)
    print(json.dumps(proofs))


if __name__ == "__main__":
    main()
