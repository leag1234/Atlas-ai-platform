"""Interactive setup with private, atomic configuration and bounded connection checks."""

import argparse
import getpass
import json
import os
from pathlib import Path
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


def write_private(path: Path, settings: dict[str, str]) -> None:
    if path.is_symlink():
        raise ValueError("Refusing a symlink configuration file.")
    fd, temporary = tempfile.mkstemp(prefix=".atlas-env-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            os.fchmod(output.fileno(), 0o600)
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


def main() -> int:
    parser = argparse.ArgumentParser(prog="./atlas")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="configure and start the Docker trial")
    setup.add_argument("--provider", choices=list(presets()))
    setup.add_argument("--api-key-file", type=Path)
    setup.add_argument("--model")
    setup.add_argument(
        "--http-port",
        type=http_port,
        default=os.environ.get("ATLAS_HTTP_PORT", "3000"),
        help="published HTTP port (ATLAS_HTTP_PORT; default: 3000)",
    )
    setup.add_argument("--base-url")
    setup.add_argument("--yes", action="store_true")
    setup.add_argument("--no-start", action="store_true")
    args = parser.parse_args()
    os.chdir(ROOT)
    try:
        subprocess.run(
            ["docker", "compose", "version"], check=True, stdout=subprocess.DEVNULL
        )
        destination = ROOT / ".env"
        if destination.exists() or destination.is_symlink():
            raise ValueError(
                ".env already exists. Back it up and move it before running setup again."
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
            "ATLAS_TEXT_MODEL": model
            if model != preset.get("text_model") or provider != "scaleway"
            else "",
            "ATLAS_TEXT_FALLBACK_MODEL": preset.get("text_fallback_model", ""),
            "ATLAS_LOCAL_MODEL": model
            if provider == "local"
            else presets()["local"]["text_model"],
            "WEBUI_SECRET_KEY": secrets.token_urlsafe(32),
            "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
            "SEARXNG_SECRET": secrets.token_urlsafe(32),
            "ATLAS_HTTP_PORT": args.http_port,
            "ATLAS_OLLAMA_PORT": os.environ.get("ATLAS_OLLAMA_PORT", "13702"),
        }
        temporary = ROOT / ".atlas-setup.env"
        compose = ["docker", "compose"]
        if provider == "local":
            print(
                "Starting Ollama without GPU; pulling the preset on first start. CPU answers are slower and weaker."
            )
            settings["COMPOSE_PROFILES"] = "local"
            write_private(temporary, settings)
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
                )
                local_base = "http://127.0.0.1:" + settings["ATLAS_OLLAMA_PORT"] + "/v1"
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
                    )
                temporary.unlink(missing_ok=True)
        else:
            check_connection(base, key, model, provider)
        write_private(destination, settings)
        print("Saved .env with mode 600. Provider credentials were not displayed.")
        if (
            not preset.get("price")
            or preset["price"]["currency"] != "EUR"
            or args.model
        ):
            print(
                "WARNING: no verified EUR price for this configuration; monetary budget ceiling disabled. Provider billing still applies."
            )
        start = not args.no_start and (
            args.yes or input("Start Atlas now? [Y/n]: ").strip().lower() != "n"
        )
        if start:
            subprocess.run(
                compose + ["up", "-d", "--build", "--wait", "--wait-timeout", "600"],
                check=True,
            )
        if not start and provider == "local":
            subprocess.run(compose + ["stop", "ollama"], check=True)
        print(
            "Open http://localhost:"
            + settings["ATLAS_HTTP_PORT"]
            + " and create the first administrator account."
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
