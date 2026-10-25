"""Provider setup contracts: legacy isolation, prices and credential handling."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from provider_settings import configure, endpoint, presets, request_options
from serverless import ServerlessPolicy
from scripts.atlas_setup import env_value, write_private


class ProviderSetupTests(unittest.TestCase):
    def test_all_required_presets(self) -> None:
        self.assertEqual(
            set(presets()),
            {
                "hetzner",
                "scaleway",
                "mistral",
                "ionos",
                "ovhcloud",
                "local",
                "huggingface",
                "deepseek",
                "other",
            },
        )

    def test_legacy_policy_unchanged(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            policy = ServerlessPolicy()
            self.assertEqual(policy.models["text"], policy.config["text"]["model"])
            self.assertGreater(policy.cost(policy.models["text"], 1000, 10), 0)

    def test_unpriced_override_and_generic_roles(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ATLAS_PROVIDER": "other",
                "ATLAS_TEXT_MODEL": "operator-model",
                "ATLAS_PROVIDER_BASE_URL": "https://provider.example/v1",
            },
            clear=True,
        ):
            with self.assertLogs("provider_settings", level="WARNING") as logs:
                policy = ServerlessPolicy()
            self.assertIn("ceiling disabled", " ".join(logs.output))
            self.assertEqual(policy.models["code"], "operator-model")
            self.assertEqual(policy.cost("operator-model", 1000000, 1000000), 0)
            self.assertEqual(endpoint(), "https://provider.example/v1")

    def test_zero_price_is_verified_free(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PROVIDER": "hetzner"}, clear=True):
            policy = ServerlessPolicy()
            self.assertFalse(policy.prices[policy.models["text"]]["unpriced"])

    def test_currency_conversion_is_explicit(self) -> None:
        with patch.dict(
            os.environ,
            {"ATLAS_PROVIDER": "mistral", "ATLAS_USD_TO_EUR": "0.9"},
            clear=True,
        ):
            policy = ServerlessPolicy()
            self.assertGreater(policy.cost(policy.models["text"], 1000, 10), 0)

    def test_ovh_context_is_selected_per_role(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PROVIDER": "ovhcloud"}, clear=True):
            policy = ServerlessPolicy()
        self.assertEqual(
            policy.config["text"]["capabilities"][policy.models["text"]]["context"],
            262144,
        )
        self.assertEqual(
            policy.config["vision"]["capabilities"][policy.models["vision"]]["context"],
            32768,
        )

    def test_mistral_fallback_is_configured_unpriced_and_can_be_disabled(self) -> None:
        with patch.dict(
            os.environ,
            {"ATLAS_PROVIDER": "mistral", "ATLAS_USD_TO_EUR": "0.9"},
            clear=True,
        ):
            policy = ServerlessPolicy()
            self.assertEqual(policy.models["text_fallback"], "mistral-medium-latest")
            self.assertTrue(policy.prices[policy.models["text_fallback"]]["unpriced"])
        with patch.dict(
            os.environ,
            {
                "ATLAS_PROVIDER": "mistral",
                "ATLAS_TEXT_FALLBACK_MODEL": "",
                "ATLAS_USD_TO_EUR": "0.9",
            },
            clear=True,
        ):
            policy = ServerlessPolicy()
            self.assertNotIn("text_fallback", policy.models)

    def test_remote_http_and_embedded_credentials_rejected(self) -> None:
        for url in (
            "http://provider.example/v1",
            "https://user:password@provider.example/v1",
        ):
            with patch.dict(
                os.environ,
                {"ATLAS_PROVIDER": "other", "ATLAS_PROVIDER_BASE_URL": url},
                clear=True,
            ):
                with self.assertRaises(ValueError):
                    endpoint()

    def test_local_http_and_vendor_options(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PROVIDER": "local"}, clear=True):
            self.assertTrue(endpoint().startswith("http://ollama:"))
            self.assertEqual(
                request_options(
                    {"reasoning_effort": "none", "tools": [], "model": "chosen"}
                ),
                {"model": "chosen"},
            )

    def test_private_file_and_symlink_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            write_private(path, {"ATLAS_PROVIDER_API_KEY": "literal$dollar#value"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("'literal$dollar#value'", path.read_text())
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                write_private(link, {})
        with self.assertRaises(ValueError):
            env_value("one\nINJECT=two")

    def test_no_configuration_change_without_opt_in(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config: dict[str, object] = {"text": {}}
            prices: dict[str, object] = {"existing": {}}
            self.assertEqual(configure(config, prices), (config, prices))


class TrialBoundaryTests(unittest.TestCase):
    def test_trial_cannot_enable_gpu(self) -> None:
        from packages.gpu_ownership import gpu_allowed, image_worker_address

        with patch.dict(
            os.environ,
            {
                "ATLAS_TRIAL": "1",
                "ATLAS_DEV_GPU": "1",
                "ATLAS_STABLE": "1",
                "ATLAS_IMAGE_GPU_IP": "configured-host",
            },
        ):
            self.assertFalse(gpu_allowed())
            self.assertEqual(image_worker_address(), "")

    def test_compose_only_publishes_trial_ports(self) -> None:
        import yaml

        compose = yaml.safe_load(Path("compose.yaml").read_text())
        services = compose["services"]
        self.assertEqual(
            set(name for name, value in services.items() if "ports" in value),
            {"devapi", "frontend", "ollama"},
        )
        self.assertEqual(services["devapi"]["profiles"], ["api"])
        self.assertTrue(
            services["devapi"]["ports"][0].startswith("${ATLAS_API_BIND:-127.0.0.1}:")
        )
        self.assertEqual(services["ollama"]["profiles"], ["local"])
        self.assertEqual(
            services["orchestrator"]["environment"]["ATLAS_SEARCH_PROVIDER"], "searxng"
        )
        for service in services.values():
            self.assertNotIn("privileged", service)
            self.assertNotIn("gpus", service)
            self.assertNotIn("/var/run/docker.sock", str(service.get("volumes", [])))

    def test_setup_rejects_bad_connection_without_echoing_key(self) -> None:
        import io
        from contextlib import redirect_stdout
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading
        from scripts.atlas_setup import check_connection

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(401)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaises(ValueError) as raised:
                check_connection(
                    f"http://127.0.0.1:{server.server_port}",
                    "private-test-value",
                    "model",
                )
            self.assertNotIn(
                "private-test-value", str(raised.exception) + output.getvalue()
            )
            self.assertIn("HTTP 401", str(raised.exception))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_trial_feature_errors_are_clear(self) -> None:
        from fastapi.testclient import TestClient
        from services.orchestrator.chat_api import app

        with patch.dict(os.environ, {"ATLAS_TRIAL": "1"}), TestClient(app) as client:
            response = client.post("/v1/chat/completions", json={"code_mode": True})
            self.assertEqual(response.status_code, 503)
            self.assertIn("Code mode is unavailable", response.json()["message"])
            response = client.post("/v1/audio/transcriptions")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(
                response.json()["message"],
                "Speech is not enabled on this installation.",
            )
