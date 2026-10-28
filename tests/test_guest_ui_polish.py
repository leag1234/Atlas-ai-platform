"""Guest shell configuration contracts; browser coverage has a separate runner."""

import os
import unittest
from unittest.mock import patch

from services.orchestrator.serving import stable_webui_options
from services.orchestrator.webui import managed_model


class GuestSettingsTests(unittest.TestCase):
    def test_stable_model_disables_generic_suggestions(self) -> None:
        self.assertEqual(managed_model("atlas-qwen")["meta"]["suggestion_prompts"], [])

    def test_stable_locale_uses_browser_preference(self) -> None:
        with patch.dict(
            os.environ,
            {"ATLAS_STABLE": "1", "ATLAS_TRUSTED_EMAIL_HEADER": "X-Atlas-User"},
        ):
            self.assertIn("DEFAULT_LOCALE=", stable_webui_options())
