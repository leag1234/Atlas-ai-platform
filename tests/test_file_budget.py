"""Owner file-production cap applies to generation, never ordinary discussion."""

from decimal import Decimal
import unittest

from packages.file_intent import produces_file
from packages.profiles import PROFILES
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.loop import Limits
from agent_provider import AgentRequest


class FileBudgetTests(unittest.TestCase):
    def test_natural_requests_and_negative_discussion(self) -> None:
        for text in (
            "Create a PDF",
            "Fais une présentation",
            "Export these results to a file",
            "MAKE A SPREADSHEET",
            "Erstelle eine Tabelle als Datei",
            "Genera un documento",
            "prepare un diaporama",
            "PDF please",
        ):
            with self.subTest(text=text):
                self.assertTrue(produces_file(text))
        for text in ("Explain what PDF means", "What is a spreadsheet?", "Hello"):
            self.assertFalse(produces_file(text))

    def test_generation_allowance_is_shared_and_bounded(self) -> None:
        model = GatewayModel(
            "http://localhost",
            Configuration(
                input_eur_per_mtok=Decimal(1),
                output_eur_per_mtok=Decimal(1),
                max_tokens=2048,
            ),
        )
        model.configure_quality(PROFILES[0], 6000, produces_files=True)
        model.spent = Decimal("0.08")
        self.assertEqual(model.allowance, Decimal("0.22"))
        Limits(profile=PROFILES[0], produces_files=True, cost=Decimal("0.30"))
        payload = dict(
            messages=[{"role": "user", "content": "Create a PDF"}],
            tools=[],
            timeout=120.0,
            profile=PROFILES[0],
            budget_eur="0.30",
        )
        AgentRequest.model_validate({**payload, "produces_files": True})
        with self.assertRaises(ValueError):
            AgentRequest.model_validate(payload)
        with self.assertRaises(ValueError):
            Limits(profile=PROFILES[0], cost=Decimal("0.30"))
        with self.assertRaises(ValueError):
            Limits(profile=PROFILES[0], produces_files=True, cost=Decimal("0.31"))
