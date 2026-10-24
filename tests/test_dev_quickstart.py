"""Developer quick start uses the chat presets and keeps credentials private."""

import argparse
from collections.abc import AsyncIterator
from contextlib import redirect_stdout
from email.message import Message
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import yaml
import dev_gateway as gateway
from packages.dev_request import Request
from packages.limits import LimitError
from provider_settings import presets, request_options
from provider_retry import TransientProviderError
from scripts import atlas_setup
from serverless import ServerlessPolicy
from services.orchestrator.dev_auth import Store, provision, QuotaError


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def request(self, **options: Any) -> Request:
        return Request.model_validate(
            {
                "messages": [
                    {"role": "user", "content": "Reply with OK."},
                    {"role": "system", "content": "Be concise."},
                ],
                **options,
            }
        )

    def test_every_preset_matches_chat_options(self) -> None:
        for provider in presets():
            with (
                self.subTest(provider=provider),
                patch.dict(
                    os.environ,
                    {
                        "ATLAS_PROVIDER": provider,
                        "ATLAS_PROVIDER_BASE_URL": "https://example.invalid/v1",
                        **(
                            {"ATLAS_TEXT_MODEL": "quickstart-test-model"}
                            if provider == "other"
                            else {}
                        ),
                    },
                    clear=True,
                ),
            ):
                plan = gateway.prepare(self.request(reasoning_effort="low"))
                policy = ServerlessPolicy()
                body = json.loads(plan.body)
                self.assertEqual(body["model"], policy.models["code"])
                self.assertEqual(plan.endpoint, "https://example.invalid/v1")
                self.assertEqual(body, request_options(body))
                self.assertEqual(body["stream_options"], {"include_usage": True})
                self.assertNotIn("tools", body)
                if provider == "hetzner":
                    self.assertEqual(
                        [m["role"] for m in body["messages"]], ["system", "user"]
                    )
                    self.assertIn("Be concise.", body["messages"][0]["content"])
                    self.assertEqual(body["reasoning_effort"], "none")
                elif provider == "scaleway":
                    self.assertEqual(body["reasoning_effort"], "low")
                else:
                    self.assertNotIn("reasoning_effort", body)
                if provider in (
                    "hetzner",
                    "local",
                    "mistral",
                    "ovhcloud",
                    "ionos",
                    "other",
                ):
                    self.assertEqual(plan.reserved, 0)
                if provider == "mistral":
                    self.assertIsNotNone(plan.fallback)

    def test_tools_schema_and_context_per_role(self) -> None:
        tool = {
            "type": "function",
            "function": {"name": "lookup", "parameters": {"type": "object"}},
        }
        schema = {
            "type": "json_schema",
            "json_schema": {"name": "value", "schema": {"type": "object"}},
        }
        for provider in presets():
            with (
                self.subTest(provider=provider),
                patch.dict(
                    os.environ,
                    {
                        "ATLAS_PROVIDER": provider,
                        "ATLAS_TEXT_MODEL": "custom",
                        "ATLAS_PROVIDER_BASE_URL": "https://example.invalid/v1",
                    },
                    clear=True,
                ),
            ):
                body = json.loads(
                    gateway.prepare(
                        self.request(
                            tools=[tool], tool_choice="required", response_format=schema
                        )
                    ).body
                )
                self.assertEqual(body["tools"][0]["function"]["name"], "lookup")
                self.assertEqual(body["tool_choice"], "required")
                self.assertEqual(
                    body["response_format"]["json_schema"]["name"], "value"
                )
                with patch.dict(os.environ, {"ATLAS_MODEL_CONTEXT": "100"}):
                    with self.assertRaisesRegex(LimitError, "context_exceeded"):
                        gateway.prepare(self.request())
        with patch.dict(
            os.environ,
            {"ATLAS_PROVIDER": "ovhcloud", "ATLAS_TEXT_FALLBACK_MODEL": "custom"},
            clear=True,
        ):
            plan = gateway.prepare(
                Request.model_validate(
                    {"messages": [{"role": "user", "content": "x" * 40000}]}
                )
            )
            self.assertIsNotNone(plan.fallback)
        with patch.dict(os.environ, {"ATLAS_PROVIDER": "local"}, clear=True):
            self.assertTrue(
                gateway.prepare(self.request()).endpoint.startswith("http://ollama:")
            )

    async def test_typed_stream_and_transient_fallback_only_before_output(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PROVIDER": "mistral"}, clear=True):
            plan = gateway.prepare(self.request())
            models: list[str] = []
            raw = (
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": [{"type": "text", "text": "OK"}]},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 4, "completion_tokens": 1},
                    }
                )
                + "\n\ndata: [DONE]\n\n"
            ).encode()

            async def chunks(current: gateway.Plan) -> AsyncIterator[bytes]:
                models.append(json.loads(current.body)["model"])
                if current is plan:
                    raise TransientProviderError("provider_error")
                yield raw

            with patch.object(gateway, "transport", chunks):
                frames = [frame async for frame in gateway.events(plan)]
            self.assertEqual(
                models,
                [
                    json.loads(plan.body)["model"],
                    ServerlessPolicy().models["text_fallback"],
                ],
            )
            self.assertEqual(frames[0]["choices"][0]["delta"]["content"], "OK")
            self.assertEqual(frames[-1]["usage"]["completion_tokens"], 1)
            self.assertEqual(frames[-1]["cost_micro_eur"], 0)
            models.clear()

            async def partial(current: gateway.Plan) -> AsyncIterator[bytes]:
                models.append(json.loads(current.body)["model"])
                yield b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
                raise TransientProviderError("provider_error")

            with (
                patch.object(gateway, "transport", partial),
                self.assertRaises(TransientProviderError),
            ):
                _ = [frame async for frame in gateway.events(plan)]
            self.assertEqual(len(models), 1)

    def test_free_unpriced_request_quotas_survive_restart_and_failed_calls(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "usage.sqlite")
            provision(store, "client", root / "client.key", 3, 1)
            paid = store.reserve("client", 1)
            store.finish(paid, None)
            for _ in range(2):
                reservation = store.reserve("client", 0, monetary=False)
                store.finish(reservation, None)
            with self.assertRaises(QuotaError):
                Store(store.path).reserve("client", 0, monetary=False)
            self.assertEqual(store.usage("client")["requests"], 3)
            self.assertEqual(store.usage("client")["charged_micro_eur"], 1)


class SetupTests(unittest.TestCase):
    def test_compose_opt_in_loopback_persistent_store_and_shared_provider(self) -> None:
        config = yaml.safe_load(Path("compose.yaml").read_text())
        service = config["services"]["devapi"]
        self.assertEqual(service["profiles"], ["api"])
        self.assertEqual(service["command"], ["devapi"])
        self.assertEqual(
            service["ports"],
            ["${ATLAS_API_BIND:-127.0.0.1}:${ATLAS_API_PORT:-3001}:13731"],
        )
        self.assertEqual(service["volumes"], ["devapi:/app/BRAIN/devapi"])
        self.assertIn("devapi", config["volumes"])
        for name, value in config["services"]["gateway"]["environment"].items():
            self.assertEqual(service["environment"][name], value)
        self.assertEqual(
            service["healthcheck"]["test"],
            ["CMD", "python", "/app/entrypoint.py", "devapi-health"],
        )

    def test_key_commands_never_display_keys_without_show(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("COMPOSE_PROJECT_NAME=isolated\n")
            store = Store(root / "database")

            def run(
                command: list[str], **kwargs: Any
            ) -> subprocess.CompletedProcess[str]:
                self.assertIn("--no-deps", command)
                self.assertIn("devapi", command)
                operation = command[command.index("services.orchestrator.dev_auth") + 1]
                if operation == "create":
                    provision(
                        store, "alice", root / "BRAIN/api-keys/alice.key", 7, 50000
                    )
                    return subprocess.CompletedProcess(command, 0, "")
                if operation == "revoke":
                    store.revoke("alice")
                return subprocess.CompletedProcess(
                    command, 0, '[{"id":"alice","enabled":1,"daily_requests":7}]'
                )

            with (
                patch.object(atlas_setup, "ROOT", root),
                patch("scripts.atlas_setup.subprocess.run", side_effect=run),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    atlas_setup.api_key_command(
                        argparse.Namespace(
                            operation="create",
                            name="alice",
                            daily_requests=7,
                            show=False,
                        )
                    )
                    atlas_setup.api_key_command(argparse.Namespace(operation="list"))
                    atlas_setup.api_key_command(
                        argparse.Namespace(operation="revoke", name="alice")
                    )
                key = (root / "BRAIN/api-keys/alice.key").read_text().strip()
                self.assertNotIn(key, output.getvalue())
                self.assertIn("mode 600", output.getvalue())
                self.assertIn("daily requests: 7", output.getvalue())
                with self.assertRaises(PermissionError):
                    store.authenticate(key)
                (root / "BRAIN/api-keys/alice.key").unlink()
                with store.connection() as db:
                    db.execute("DELETE FROM developers")
                output = io.StringIO()
                with redirect_stdout(output):
                    atlas_setup.api_key_command(
                        argparse.Namespace(
                            operation="create",
                            name="alice",
                            daily_requests=7,
                            show=True,
                        )
                    )
                self.assertIn(
                    (root / "BRAIN/api-keys/alice.key").read_text().strip(),
                    output.getvalue(),
                )

    def test_setup_models_readiness_requires_authentication_response(self) -> None:
        for status, kind, accepted in (
            (401, "authentication_error", True),
            (404, "not_found", False),
            (401, "other", False),
        ):
            exc = HTTPError(
                "http://localhost/v1/models",
                status,
                "test",
                Message(),
                io.BytesIO(json.dumps({"error": {"type": kind}}).encode()),
            )
            with (
                patch.object(atlas_setup, "build_opener") as opener,
                redirect_stdout(io.StringIO()),
            ):
                opener.return_value.open.side_effect = exc
                if accepted:
                    atlas_setup.check_devapi("19301")
                else:
                    with self.assertRaises(ValueError):
                        atlas_setup.check_devapi("19301")
        with patch.object(atlas_setup, "build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(b"{}")
            with self.assertRaisesRegex(ValueError, "unauthenticated"):
                atlas_setup.check_devapi("19301")

    def test_setup_api_profile_and_reconfiguration_restart(self) -> None:
        cwd = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                key = root / "provider-key"
                key.write_text("test-only")
                output = io.StringIO()
                with (
                    patch.object(atlas_setup, "ROOT", root),
                    patch.object(atlas_setup, "check_connection"),
                    patch.object(atlas_setup, "check_devapi") as health,
                    patch("scripts.atlas_setup.subprocess.run") as run,
                    redirect_stdout(output),
                ):
                    base = [
                        "atlas",
                        "setup",
                        "--provider",
                        "hetzner",
                        "--api-key-file",
                        str(key),
                        "--yes",
                        "--http-port",
                        "19300",
                    ]
                    with patch("sys.argv", base + ["--api", "--api-port", "19301"]):
                        self.assertEqual(atlas_setup.main(), 0)
                    _, settings = atlas_setup.read_existing(root / ".env")
                    self.assertEqual(settings["COMPOSE_PROFILES"], "api")
                    health.assert_called_with("19301")
                    with patch("sys.argv", base + ["--reconfigure"]):
                        self.assertEqual(atlas_setup.main(), 0)
                    self.assertEqual(run.call_args.args[0][-2:], ["gateway", "devapi"])
                    self.assertNotIn("test-only", output.getvalue())
        finally:
            os.chdir(cwd)


class CumulativeUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_cumulative_usage_only_settles_once_and_never_decreases(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PROVIDER": "hetzner"}, clear=True):
            plan = gateway.prepare(
                Request.model_validate(
                    {
                        "messages": [{"role": "user", "content": "Add 2 and 3."}],
                        "max_tokens": 8,
                    }
                )
            )
            for counts, accepted in (
                ([0, 1, 2, 2], True),
                ([0, 2, 1, 2], False),
                ([0, 1, 2, 9], False),
            ):

                async def chunks(current: gateway.Plan) -> AsyncIterator[bytes]:
                    for index, count in enumerate(counts):
                        frame = {
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": "5" if index == 1 else ""},
                                    "finish_reason": "stop" if index == 2 else None,
                                }
                            ]
                            if index < 3
                            else [],
                            "usage": {"prompt_tokens": 60, "completion_tokens": count},
                        }
                        yield ("data: " + json.dumps(frame) + "\n\n").encode()
                    yield b"data: [DONE]\n\n"

                with patch.object(gateway, "transport", chunks):
                    if accepted:
                        frames = [frame async for frame in gateway.events(plan)]
                        self.assertEqual(sum("usage" in frame for frame in frames), 1)
                        self.assertEqual(
                            frames[-1]["usage"],
                            {"prompt_tokens": 60, "completion_tokens": 2},
                        )
                    else:
                        with self.assertRaises(ValueError):
                            _ = [frame async for frame in gateway.events(plan)]


class ProviderTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_credentials_and_transient_status_classification(
        self,
    ) -> None:
        from unittest.mock import MagicMock
        import aiohttp

        for status in (200, 401, 429, 503):
            with patch.dict(
                os.environ,
                {
                    "ATLAS_PROVIDER": "other",
                    "ATLAS_TEXT_MODEL": "custom",
                    "ATLAS_PROVIDER_BASE_URL": "https://chosen.invalid/v1",
                    "ATLAS_PROVIDER_API_KEY": "selected-test-value",
                    "SCW_GENERATIVE_API_KEY": "legacy-test-value",
                },
                clear=True,
            ):
                plan = gateway.prepare(
                    Request.model_validate(
                        {"messages": [{"role": "user", "content": "Hi"}]}
                    )
                )
                with patch.object(aiohttp, "ClientSession") as factory:
                    factory.return_value.__aenter__.return_value = MagicMock()
                    session = factory.return_value.__aenter__.return_value
                    session.post.return_value.__aenter__.return_value = MagicMock()
                    response = session.post.return_value.__aenter__.return_value
                    response.status = status

                    async def chunks(size: int) -> AsyncIterator[bytes]:
                        yield b"fragment"

                    response.content.iter_chunked.side_effect = chunks
                    if status == 200:
                        self.assertEqual(
                            [piece async for piece in gateway.transport(plan)],
                            [b"fragment"],
                        )
                    elif status == 401:
                        with self.assertRaises(RuntimeError) as caught:
                            _ = [piece async for piece in gateway.transport(plan)]
                        self.assertNotIsInstance(
                            caught.exception, TransientProviderError
                        )
                    else:
                        with self.assertRaises(TransientProviderError):
                            _ = [piece async for piece in gateway.transport(plan)]
                    self.assertEqual(
                        session.post.call_args.args[0],
                        "https://chosen.invalid/v1/chat/completions",
                    )
                    self.assertEqual(
                        session.post.call_args.kwargs["headers"]["Authorization"],
                        "Bearer selected-test-value",
                    )


class KeyOwnershipTests(unittest.TestCase):
    def test_container_key_is_assigned_to_invoking_host_user(self) -> None:
        from services.orchestrator import dev_auth

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.dict(os.environ, {"ATLAS_DEVAPI_DB": str(root / "usage.sqlite")}),
                patch(
                    "sys.argv",
                    [
                        "dev_auth",
                        "create",
                        "editor",
                        "--key-file",
                        str(root / "editor.key"),
                        "--key-owner",
                        "1234",
                        "2345",
                    ],
                ),
                patch("services.orchestrator.dev_auth.os.chown") as chown,
                redirect_stdout(io.StringIO()) as output,
            ):
                dev_auth.main()
            chown.assert_called_once_with(
                root / "editor.key", 1234, 2345, follow_symlinks=False
            )
            self.assertEqual((root / "editor.key").stat().st_mode & 0o777, 0o600)
            self.assertEqual(output.getvalue(), "")
