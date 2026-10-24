"""Interactive setup with private, atomic configuration and bounded connection checks."""

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import warnings
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from provider_settings import presets, request_options, validate_endpoint
from setup_stream import read_completion

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_SETTINGS = {
    "ATLAS_PROVIDER",
    "ATLAS_PROVIDER_BASE_URL",
    "ATLAS_PROVIDER_API_KEY",
    "ATLAS_TEXT_MODEL",
    "ATLAS_TEXT_FALLBACK_MODEL",
    "ATLAS_VISION_MODEL",
    "ATLAS_LOCAL_MODEL",
    "ATLAS_MODEL_CONTEXT",
    "ATLAS_USD_TO_EUR",
}


def read_existing(path: Path) -> tuple[str, dict[str, str]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("--reconfigure requires an existing regular .env file.")
    source = path.read_text()
    records = re.compile(
        r"(?:[ \t]*(?:#[^\n]*)?(?:\n|$))|"
        r"[ \t]*(?:export[ \t]+)?(?P<name>[A-Za-z_][A-Za-z_0-9]*)[ \t]*=[ \t]*"
        r"(?P<value>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|(?!['\"])[^\n]*)"
        r"[ \t]*(?:#[^\n]*)?(?:\n|$)",
        re.DOTALL,
    )
    kept = []
    values = {}
    offset = 0
    while offset < len(source):
        match = records.match(source, offset)
        if match is None or match.end() == offset:
            raise ValueError("Cannot safely parse .env; configuration was not changed.")
        name = match.group("name")
        if name:
            raw = match.group("value").strip()
            values[name] = (
                raw[1:-1]
                if raw.startswith(("'", '"'))
                else re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()
            )
        if name not in PROVIDER_SETTINGS:
            kept.append(match.group())
        offset = match.end()
    return "".join(kept), values


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def check_connection(base: str, key: str, model: str, provider: str = "") -> None:
    opener = build_opener(NoRedirect)

    def call(path: str, body: dict[str, object] | None = None) -> Any:
        request = Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
            },
        )
        try:
            with opener.open(request, timeout=60) as response:
                if body is not None:
                    try:
                        return read_completion(response)
                    except (ValueError, UnicodeError):
                        raise ValueError(
                            "Provider streaming check failed: invalid or incomplete stream; "
                            "check streaming support, response IDs and cumulative token usage."
                        ) from None
                return json.loads(response.read(2_000_001))
        except HTTPError as exc:
            raise ValueError(
                f"Provider connection failed: HTTP {exc.code}; check endpoint, model and account access."
            ) from None
        except (URLError, TimeoutError, json.JSONDecodeError):
            raise ValueError(
                "Provider connection failed: transport timeout or invalid JSON during model-list/streaming check."
            ) from None

    data = call("/models")
    ids = {row.get("id") for row in data.get("data", []) if isinstance(row, dict)}
    if model not in ids:
        raise ValueError(
            "Selected model is absent from the provider model list; use --model with a current model ID."
        )
    message = call(
        "/chat/completions",
        request_options(
            {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 1024,
                **(
                    {"reasoning_effort": "none"}
                    if provider in ("scaleway", "local")
                    else {}
                ),
                "stream": True,
                "stream_options": {"include_usage": True},
            },
            provider,
        ),
    )
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        if any(
            isinstance(message.get(field), str) and message[field].strip()
            for field in ("reasoning_content", "reasoning")
        ):
            print(
                "WARNING: provider reachable but returned reasoning only; no visible answer was verified."
            )
            return
        raise ValueError(
            "Provider returned an empty completion; choose an instruction model."
        )
    print("Model list and tiny streaming completion verified.")


def env_value(value: str) -> str:
    if any(c in value for c in "\r\n\0"):
        raise ValueError("Configuration values must be a single line.")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def write_private(path: Path, settings: dict[str, str], preserved: str = "") -> None:
    if path.is_symlink():
        raise ValueError("Refusing a symlink configuration file.")
    fd, temporary = tempfile.mkstemp(prefix=".atlas-env-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            if preserved:
                output.write(preserved + ("" if preserved.endswith("\n") else "\n"))
            else:
                output.write("# Atlas setup; keep private.\n")
            for name, value in settings.items():
                output.write(name + "=" + env_value(value) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def http_port(value: str) -> str:
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 65535:
        raise argparse.ArgumentTypeError(
            "HTTP port must be an integer from 1 to 65535."
        )
    return str(int(value))


def check_devapi(port: str) -> None:
    # No key is created for readiness: test the authenticated endpoint's boundary.
    try:
        with build_opener(NoRedirect).open(
            "http://127.0.0.1:" + port + "/v1/models", timeout=5
        ):
            raise ValueError(
                "Developer API unexpectedly permits unauthenticated access."
            )
    except HTTPError as exc:
        if (
            exc.code == 401
            and json.loads(exc.read(4096)).get("error", {}).get("type")
            == "authentication_error"
        ):
            print(
                "Developer API /v1/models verified (401; create a key to authenticate)."
            )
            return
        raise ValueError(
            "Developer API readiness failed: expected authentication response."
        ) from None
    except (URLError, TimeoutError):
        raise ValueError("Developer API /v1/models is unavailable.") from None


def api_key_command(args: argparse.Namespace) -> None:
    if args.operation != "list" and not re.fullmatch(
        r"[a-zA-Z0-9_-]{1,64}", args.name or ""
    ):
        raise ValueError(
            "Key name must contain 1-64 ASCII letters, digits, underscores or dashes."
        )
    _, existing = read_existing(ROOT / ".env")
    environment = {k: v for k, v in os.environ.items() if k not in existing}
    command = [
        "docker",
        "compose",
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--entrypoint",
        "python",
    ]
    keyfile = None
    if args.operation == "create":
        if args.daily_requests <= 0:
            raise ValueError("Daily requests must be positive.")
        directory = ROOT / "BRAIN" / "api-keys"
        if directory.is_symlink():
            raise ValueError("Refusing a symlink key directory.")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        keyfile = directory / (args.name + ".key")
        if keyfile.exists() or keyfile.is_symlink():
            raise ValueError("Key file already exists; choose another name.")
        command += ["--volume", str(directory) + ":/keys"]
    command += ["devapi", "-m", "services.orchestrator.dev_auth", args.operation]
    if args.operation != "list":
        command += [args.name]
    if keyfile:
        command += [
            "--key-file",
            "/keys/" + keyfile.name,
            "--daily-requests",
            str(args.daily_requests),
            "--key-owner",
            str(os.getuid()),
            str(os.getgid()),
        ]
    result = subprocess.run(
        command, check=True, capture_output=True, text=True, env=environment
    )
    if keyfile:
        if keyfile.stat().st_mode & 0o777 != 0o600:
            raise ValueError("Key file permissions must be 600.")
        print("Key file written to " + str(keyfile) + " (mode 600).")
        if args.show:
            print(keyfile.read_text().strip())
    elif args.operation == "list":
        for row in json.loads(result.stdout):
            print(
                f"{row['id']}  {'enabled' if row['enabled'] else 'revoked'}  daily requests: {row['daily_requests']}"
            )
    else:
        print("Key revoked: " + args.name)


def main() -> int:
    parser = argparse.ArgumentParser(prog="./atlas")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="configure and start the Docker trial")
    setup.add_argument(
        "--api", action="store_true", help="enable the authenticated developer API"
    )
    setup.add_argument(
        "--api-port", type=http_port, default=os.environ.get("ATLAS_API_PORT", "3001")
    )
    setup.add_argument("--provider", choices=list(presets()))
    setup.add_argument("--api-key-file", type=Path)
    setup.add_argument("--model")
    setup.add_argument(
        "--reconfigure",
        action="store_true",
        help="change provider while preserving secrets and saved data",
    )
    setup.add_argument(
        "--http-port",
        type=http_port,
        default=os.environ.get("ATLAS_HTTP_PORT", "3000"),
        help="published HTTP port (ATLAS_HTTP_PORT; default: 3000)",
    )
    setup.add_argument("--base-url")
    setup.add_argument("--yes", action="store_true")
    setup.add_argument("--no-start", action="store_true")
    keys = commands.add_parser(
        "api-key", help="manage developer keys without displaying secrets"
    )
    operations = keys.add_subparsers(dest="operation", required=True)
    create = operations.add_parser("create")
    create.add_argument("name")
    create.add_argument("--daily-requests", type=int, default=20)
    create.add_argument("--show", action="store_true")
    operations.add_parser("list")
    revoke = operations.add_parser("revoke")
    revoke.add_argument("name")
    args = parser.parse_args()
    os.chdir(ROOT)
    try:
        subprocess.run(
            ["docker", "compose", "version"], check=True, stdout=subprocess.DEVNULL
        )
        if args.command == "api-key":
            api_key_command(args)
            return 0
        destination = ROOT / ".env"
        preserved = ""
        existing: dict[str, str] = {}
        if args.reconfigure:
            preserved, existing = read_existing(destination)
        elif destination.exists() or destination.is_symlink():
            raise ValueError(
                ".env already exists. Use ./atlas setup --reconfigure to change provider safely."
            )
        provider = args.provider
        if not provider:
            if args.yes:
                raise ValueError("--yes requires --provider.")
            names = list(presets())
            for number, name in enumerate(names, 1):
                print(f"{number}. {presets()[name]['name']}")
            choice = int(input("Provider number: "))
            if not 1 <= choice <= len(names):
                raise ValueError("Invalid provider number.")
            provider = names[choice - 1]
        preset = presets()[provider]
        print(preset["name"])
        if preset.get("unverified"):
            print("Unverified: " + "; ".join(preset["unverified"]))
        model = args.model or preset.get("text_model")
        if not args.yes:
            model = input(f"Text model [{model or 'required'}]: ").strip() or model
        if not model:
            raise ValueError("A text model is required; supply --model.")
        base = args.base_url or preset.get("base_url")
        if not base and not args.yes:
            base = input("OpenAI-compatible base URL: ").strip()
        base = validate_endpoint(base or "", provider == "local")
        if provider == "local":
            key = "ollama"
        elif args.api_key_file:
            key = args.api_key_file.read_text().strip()
        elif not args.yes:
            if not sys.stdin.isatty():
                raise ValueError(
                    "Hidden key entry requires a terminal; use --api-key-file for automation."
                )
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = getpass.getpass("Provider API key (hidden): ").strip()
        else:
            raise ValueError("Non-interactive remote setup requires --api-key-file.")
        if not key or any(c in key for c in "\r\n\0"):
            raise ValueError("API key is empty or invalid.")
        settings = {
            "ATLAS_PROVIDER": provider,
            "ATLAS_PROVIDER_BASE_URL": base,
            "ATLAS_PROVIDER_API_KEY": key,
            "ATLAS_VISION_MODEL": preset.get("vision_model") or "",
            "ATLAS_TEXT_MODEL": model
            if model != preset.get("text_model") or provider != "scaleway"
            else "",
            "ATLAS_TEXT_FALLBACK_MODEL": preset.get("text_fallback_model", ""),
            "ATLAS_LOCAL_MODEL": model
            if provider == "local"
            else presets()["local"]["text_model"],
        }
        if not args.reconfigure:
            settings.update(
                {
                    "WEBUI_SECRET_KEY": secrets.token_urlsafe(32),
                    "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
                    "SEARXNG_SECRET": secrets.token_urlsafe(32),
                    "ATLAS_HTTP_PORT": args.http_port,
                    "ATLAS_OLLAMA_PORT": os.environ.get("ATLAS_OLLAMA_PORT", "13702"),
                }
            )
        profiles = set(filter(None, existing.get("COMPOSE_PROFILES", "").split(",")))
        if args.api:
            profiles.add("api")
            settings["ATLAS_API_PORT"] = existing.get("ATLAS_API_PORT", args.api_port)
        if provider == "local":
            profiles.add("local")
        if profiles:
            settings["COMPOSE_PROFILES"] = ",".join(sorted(profiles))
        api_enabled = "api" in profiles
        # An exported old credential must not override the retained .env value.
        compose_env = {
            k: v
            for k, v in os.environ.items()
            if k not in existing and k not in settings and k not in PROVIDER_SETTINGS
        }
        temporary = ROOT / ".atlas-setup.env"
        compose = ["docker", "compose"]
        if provider == "local":
            print(
                "Starting Ollama without GPU; pulling the preset on first start. CPU answers are slower and weaker."
            )
            write_private(temporary, settings, preserved)
            local_verified = False
            try:
                subprocess.run(
                    compose
                    + [
                        "--env-file",
                        str(temporary),
                        "--profile",
                        "local",
                        "up",
                        "-d",
                        "--build",
                        "ollama",
                    ],
                    check=True,
                    env=compose_env,
                )
                local_base = (
                    "http://127.0.0.1:"
                    + existing.get(
                        "ATLAS_OLLAMA_PORT", settings.get("ATLAS_OLLAMA_PORT", "13702")
                    )
                    + "/v1"
                )
                for _ in range(120):
                    ready = subprocess.run(
                        compose
                        + [
                            "--env-file",
                            str(temporary),
                            "exec",
                            "-T",
                            "ollama",
                            "test",
                            "-f",
                            "/tmp/model-ready",
                        ],
                        capture_output=True,
                        env=compose_env,
                    )
                    if ready.returncode == 0:
                        break
                    time.sleep(2)
                else:
                    raise ValueError(
                        "Ollama model is not ready after 240 seconds; inspect docker compose logs ollama."
                    )
                check_connection(local_base, key, model, "local")
                local_verified = True
            finally:
                if not local_verified:
                    subprocess.run(
                        compose + ["--env-file", str(temporary), "stop", "ollama"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        env=compose_env,
                    )
                temporary.unlink(missing_ok=True)
        else:
            check_connection(base, key, model, provider)
        write_private(destination, settings, preserved)
        print("Saved .env with mode 600. Provider credentials were not displayed.")
        if (
            not preset.get("price")
            or preset["price"]["currency"] != "EUR"
            or model != preset.get("text_model")
        ):
            print(
                "WARNING: no verified EUR price for this configuration; monetary budget ceiling disabled. Provider billing still applies."
            )
        start = not args.no_start and (
            args.yes or input("Start Atlas now? [Y/n]: ").strip().lower() != "n"
        )
        if start:
            services = (
                ["--no-deps", "--force-recreate", "gateway"]
                + (["devapi"] if api_enabled else [])
                if args.reconfigure
                else []
            )
            subprocess.run(
                compose
                + ["up", "-d", "--build", "--wait", "--wait-timeout", "600"]
                + services,
                check=True,
                env=compose_env,
            )
            if api_enabled:
                check_devapi(
                    existing.get(
                        "ATLAS_API_PORT", settings.get("ATLAS_API_PORT", "3001")
                    )
                )
        if not start and provider == "local":
            subprocess.run(compose + ["stop", "ollama"], check=True, env=compose_env)
        print(
            "Open http://localhost:"
            + existing.get("ATLAS_HTTP_PORT", settings.get("ATLAS_HTTP_PORT", "3000"))
            + (
                "; existing accounts and conversations are retained."
                if args.reconfigure
                else " and create the first administrator account."
            )
        )
        return 0
    except (
        ValueError,
        OSError,
        subprocess.CalledProcessError,
        getpass.GetPassWarning,
    ) as exc:
        # Exception payloads from subprocesses and filesystem paths may contain secrets.
        print(
            str(exc)
            if isinstance(exc, ValueError)
            else "Setup failed; check Docker access and configuration file permissions."
        )
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Setup cancelled.")
        return 130
