"""An explicit code model must reach the code role without changing other roles."""

import os
from pathlib import Path
import unittest
from unittest.mock import patch

import yaml
from provider_settings import presets
from serverless import ServerlessPolicy


class CodeModelSelectionTests(unittest.TestCase):
    def test_code_override_is_effective_and_other_roles_stay_identical(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PROVIDER": "hetzner"}, clear=True):
            before = ServerlessPolicy()
        with patch.dict(
            os.environ,
            {"ATLAS_PROVIDER": "hetzner", "ATLAS_CODE_MODEL": "operator-code"},
            clear=True,
        ):
            selected = ServerlessPolicy()
        self.assertEqual(selected.models["code"], "operator-code")
        self.assertEqual(
            {r: m for r, m in selected.models.items() if r != "code"},
            {r: m for r, m in before.models.items() if r != "code"},
        )
        self.assertTrue(selected.prices["operator-code"]["unpriced"])
        self.assertFalse(selected.prices[selected.models["text"]]["unpriced"])
        self.assertEqual(selected.config["code"]["env"], "ATLAS_CODE_MODEL")

    def test_catalog_code_model_has_explicit_price_and_context(self) -> None:
        model, metadata = next(iter(presets()["hetzner"]["code_models"].items()))
        with patch.dict(
            os.environ,
            {"ATLAS_PROVIDER": "hetzner", "ATLAS_CODE_MODEL": model},
            clear=True,
        ):
            selected = ServerlessPolicy()
        self.assertEqual(selected.models["code"], model)
        self.assertNotEqual(selected.models["text"], model)
        self.assertFalse(selected.prices[model]["unpriced"])
        self.assertEqual(
            selected.config["code"]["capabilities"][model]["context"],
            metadata["context"],
        )
        self.assertEqual(
            selected.cost(model, 1000000, 1000000),
            metadata["price"]["input"] + metadata["price"]["output"],
        )

    def test_empty_override_preserves_existing_selection(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PROVIDER": "hetzner"}, clear=True):
            before = ServerlessPolicy()
        with patch.dict(
            os.environ,
            {"ATLAS_PROVIDER": "hetzner", "ATLAS_CODE_MODEL": ""},
            clear=True,
        ):
            after = ServerlessPolicy()
        self.assertEqual(before.models, after.models)
        self.assertEqual(before.prices, after.prices)
        self.assertEqual(before.config, after.config)

    def test_compose_forwards_optional_code_model_to_gateway(self) -> None:
        services = yaml.safe_load(Path("compose.yaml").read_text())["services"]
        self.assertEqual(
            services["gateway"]["environment"]["ATLAS_CODE_MODEL"],
            "${ATLAS_CODE_MODEL:-}",
        )
        self.assertNotIn("ATLAS_CODE_MODEL", services["orchestrator"]["environment"])
