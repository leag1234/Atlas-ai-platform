"""Owner review 2026-10-27: complete guest settings translations and stable menu."""

import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from services.orchestrator.serving import stable_webui_options
from services.orchestrator.ui_injection import inject_atlas_ui

ROOT = Path(__file__).resolve().parents[1]


class GuestLocaleTests(unittest.TestCase):
    def test_shell_loads_native_translation_overrides(self) -> None:
        shell = inject_atlas_ui(b"<html><head></head></html>", stable=True)
        self.assertIn(
            b'<script type="module" src="/static/atlas/settings-ui.js"></script>',
            shell,
        )
        self.assertEqual(inject_atlas_ui(shell, stable=True), shell)

    def test_packaged_webui_installs_the_same_translation_assets(self) -> None:
        dockerfile = (ROOT / "infra/docker/Dockerfile.webui").read_text()
        for asset in ("settings-ui.js", "settings-locales.json"):
            self.assertIn(
                f"COPY services/orchestrator/{asset} /app/backend/open_webui/static/atlas/{asset}",
                dockerfile,
            )
        self.assertIn('src=\\"/static/atlas/settings-ui.js\\"', dockerfile)

    def test_all_reported_strings_have_four_language_translations(self) -> None:
        data = json.loads(
            (ROOT / "services/orchestrator/settings-locales.json").read_text()
        )
        keys = {
            "Basics",
            "Preferences",
            "Data",
            "WebUI Settings",
            "Font Family",
            "Use a local font family for the app interface.",
        }
        for locale in ("fr-FR", "de-DE", "es-ES", "it-IT"):
            with self.subTest(locale=locale):
                self.assertTrue(keys <= data[locale].keys())
                for key, value in data[locale].items():
                    self.assertTrue(value.strip(), key)
                    self.assertEqual(
                        re.findall(r"{{[^}]+}}", key),
                        re.findall(r"{{[^}]+}}", value),
                        key,
                    )
        for locale in ("fr-FR", "es-ES", "it-IT"):
            for key in (
                "Personal",
                "Keyboard",
                "Model parameters",
                "Choose the color theme used by the interface.",
                "Choose the language used for interface text.",
                "Set the default system prompt for new chats.",
                "Show or hide custom generation parameters.",
            ):
                self.assertNotEqual(data[locale][key], key)
        self.assertEqual(data["fr-FR"]["WebUI Settings"], "Réglages d’Atlas")

    def test_stable_disables_unused_native_features_not_atlas_memory_or_notes(
        self,
    ) -> None:
        with patch.dict(
            os.environ,
            {"ATLAS_STABLE": "1", "ATLAS_TRUSTED_EMAIL_HEADER": "X-Atlas-User"},
        ):
            options = stable_webui_options()
        for feature in ("MEMORIES", "CALENDAR", "AUTOMATIONS", "EASTER_EGGS"):
            self.assertIn(f"ENABLE_{feature}=False", options)
        self.assertNotIn("ENABLE_NOTES=False", options)
        self.assertNotIn("ATLAS_USER_MEMORY_ENABLED=0", options)
