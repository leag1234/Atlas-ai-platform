"""Open WebUI name and update-notice configuration contracts."""

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from services.orchestrator import serving


class WebUIBrandingTests(unittest.TestCase):
    def test_compose_trial_sets_atlas_name_and_disables_update_checks(self) -> None:
        compose = Path("compose.yaml").read_text()
        webui = compose.split("  webui:\n", 1)[1].split("  frontend:\n", 1)[0]
        self.assertIn("WEBUI_NAME: Atlas", webui)
        self.assertIn("ENABLE_VERSION_UPDATE_CHECK: 'False'", webui)

    def test_compose_trial_does_not_advertise_unavailable_speech_endpoints(
        self,
    ) -> None:
        compose = Path("compose.yaml").read_text()
        webui = compose.split("  webui:\n", 1)[1].split("  frontend:\n", 1)[0]
        self.assertIn("ENABLE_VOICE_INTERACTION: 'False'", webui)
        self.assertNotIn("AUDIO_TTS_", webui)
        self.assertNotIn("AUDIO_STT_", webui)

    def test_managed_environment_sets_atlas_name_and_disables_update_checks(
        self,
    ) -> None:
        settings = Path("infra/chat-ui.env").read_text()
        self.assertIn("WEBUI_NAME=Atlas", settings)
        self.assertIn("ENABLE_VERSION_UPDATE_CHECK=False", settings)

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
