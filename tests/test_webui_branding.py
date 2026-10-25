"""Open WebUI name and update-notice configuration contracts."""

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from services.orchestrator import serving
from services.orchestrator.webui import managed_model


class WebUIBrandingTests(unittest.TestCase):
    def test_compose_trial_exposes_one_model_and_keeps_branding_override_off(
        self,
    ) -> None:
        compose = Path("compose.yaml").read_text()
        webui = compose.split("  webui:\n", 1)[1].split("  frontend:\n", 1)[0]
        self.assertIn("WEBUI_NAME: Atlas", webui)
        self.assertIn("ENABLE_VERSION_UPDATE_CHECK: 'False'", webui)
        self.assertIn("ENABLE_EVALUATION_ARENA_MODELS: 'False'", webui)
        self.assertIn(
            "ATLAS_REMOVE_OPEN_WEBUI_SUFFIX: ${ATLAS_REMOVE_OPEN_WEBUI_SUFFIX:-0}",
            webui,
        )

    def test_managed_environment_sets_atlas_name_and_disables_update_checks(
        self,
    ) -> None:
        settings = Path("infra/chat-ui.env").read_text()
        self.assertIn("WEBUI_NAME=Atlas", settings)
        self.assertIn("ENABLE_VERSION_UPDATE_CHECK=False", settings)
        self.assertIn("ENABLE_EVALUATION_ARENA_MODELS=False", settings)
        self.assertIn("ATLAS_REMOVE_OPEN_WEBUI_SUFFIX=0", settings)

    def test_managed_stack_passes_atlas_name_and_disables_update_checks(self) -> None:
        environment = {
            "ATLAS_PORT_BASE": "16300",
            "OPEN_TERMINAL_API_KEY": "test-terminal-key",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "services.orchestrator.terminal_stack.network_gateway",
                return_value="172.18.0.1",
            ),
            patch("services.orchestrator.serving.docker_run") as docker_run,
        ):
            serving.webui("atlas-chat-ui", True)
        options = docker_run.call_args.args[2]
        self.assertIn("WEBUI_NAME=Atlas", options)
        self.assertIn("ENABLE_VERSION_UPDATE_CHECK=False", options)
        self.assertIn("ENABLE_EVALUATION_ARENA_MODELS=False", options)
        self.assertIn("ATLAS_REMOVE_OPEN_WEBUI_SUFFIX", options)

    def test_only_default_profile_is_granted_to_ordinary_users(self) -> None:
        public = managed_model("atlas-qwen")
        hidden = managed_model("atlas-glm")
        self.assertEqual(public["id"], "atlas-qwen")
        self.assertEqual(public["name"], "Atlas")
        self.assertEqual(public["access_grants"][0]["principal_id"], "*")
        self.assertEqual(hidden["id"], "atlas-glm")
        self.assertEqual(hidden["access_grants"], [])

    def test_suffix_override_changes_the_backend_name_before_bootstrap(self) -> None:
        startup = Path("infra/docker/webui-start.sh").read_text()
        self.assertIn("ATLAS_REMOVE_OPEN_WEBUI_SUFFIX:-0", startup)
        self.assertIn("WEBUI_NAME = WEBUI_NAME", startup)
        self.assertLess(
            startup.index("WEBUI_NAME = WEBUI_NAME"),
            startup.index("webui_bootstrap.py"),
        )
