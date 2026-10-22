"""M24 raises only attachment requests, preserving ordinary and developer caps."""

import unittest
from decimal import Decimal
from pydantic import ValidationError
from agent_provider import AgentRequest
from packages.profiles import PROFILES
from services.orchestrator.loop import Limits
from services.orchestrator.model import Configuration, GatewayModel


class AttachmentBudgetTests(unittest.TestCase):
    def test_gateway_requires_attachment_flag_for_higher_budget(self) -> None:
        body = dict(
            messages=[{"role": "user", "content": "Summarize"}],
            tools=[],
            timeout=120.0,
            profile=PROFILES[0],
            budget_eur="0.30",
        )
        with self.assertRaises(ValidationError):
            AgentRequest.model_validate(body)
        self.assertEqual(
            AgentRequest.model_validate({**body, "has_attachments": True}).budget_eur,
            "0.30",
        )
        with self.assertRaises(ValidationError):
            AgentRequest.model_validate(
                {**body, "budget_eur": "0.300001", "has_attachments": True}
            )
        with self.assertRaises(ValidationError):
            AgentRequest.model_validate(
                {**body, "profile": None, "has_attachments": True}
            )

    def test_loop_and_model_share_attachment_ceiling(self) -> None:
        with self.assertRaises(ValueError):
            Limits(profile=PROFILES[0], cost=Decimal("0.30"))
        Limits(profile=PROFILES[0], cost=Decimal("0.30"), has_attachments=True)
        model = GatewayModel(
            "http://localhost",
            Configuration(
                input_eur_per_mtok=Decimal(1),
                output_eur_per_mtok=Decimal(1),
                max_tokens=100,
            ),
        )
        model.configure_quality(PROFILES[0], 3000)
        self.assertEqual(model.allowance, Decimal("0.10"))
        model.configure_quality(PROFILES[0], 3000, has_attachments=True)
        self.assertEqual(model.allowance, Decimal("0.30"))
        model.spent = Decimal("0.12")
        self.assertEqual(model.allowance, Decimal("0.18"))
