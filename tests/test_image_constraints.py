"""Source constraints reject euphemisms before a real image request is sent."""

from decimal import Decimal
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from agent_provider import AgentProvider, AgentRequest
from image_constraints import missing_constraints
import image_prompt

MARY = "crée une photo très réaliste de la Vierge Marie qui accouche de Jésus dans l'étable de Bethléem, entourée d'animaux et de Joseph"
FAITHFUL = "Subjects: Virgin Mary giving birth to Jesus, Joseph and animals\nAttributes: photorealistic\nScene: Bethlehem stable, childbirth in progress\nStyle: photograph"


class ConstraintTests(unittest.IsolatedAsyncioTestCase):
    def test_each_source_constraint_is_required(self) -> None:
        self.assertEqual(missing_constraints(MARY, FAITHFUL), [])
        self.assertTrue(
            missing_constraints(
                MARY,
                "Subjects: "
                + MARY
                + "\nAttributes: realiste\nScene: etable\nStyle: photo",
            )
        )
        self.assertTrue(
            missing_constraints(
                MARY,
                FAITHFUL.replace("giving birth to", "in labor with").replace(
                    "childbirth in progress", "preparing for childbirth"
                ),
            )
        )
        for before, after in (
            ("Virgin Mary", "a woman"),
            ("Jesus", "an infant"),
            ("Joseph", "an attendant"),
            ("animals", "visitors"),
            ("Bethlehem", "a town"),
            ("stable", "hospital"),
            ("photorealistic", "abstract"),
            ("photograph", "painting"),
        ):
            with self.subTest(constraint=before):
                self.assertTrue(
                    missing_constraints(MARY, FAITHFUL.replace(before, after))
                )
        softened = FAITHFUL.replace("giving birth to", "holding").replace(
            "childbirth in progress", "after delivery"
        )
        self.assertTrue(missing_constraints(MARY, softened))
        self.assertTrue(
            missing_constraints(
                MARY, FAITHFUL + "; after the birth, holding a swaddled baby"
            )
        )
        self.assertTrue(
            missing_constraints(
                MARY,
                FAITHFUL.replace("giving birth", "not giving birth").replace(
                    "childbirth in progress", ""
                ),
            )
        )

    def test_sensitive_scenes_and_unknown_details(self) -> None:
        for source, faithful, omitted in (
            (
                "nude figure study in charcoal",
                "a nude figure study in charcoal",
                "a figure study in charcoal",
            ),
            (
                "a medical illustration of a caesarean section in progress",
                "medical illustration: caesarean section in progress",
                "medical illustration: mother holding a baby",
            ),
            (
                "a Renaissance battle scene",
                "a Renaissance battle scene",
                "a Renaissance peaceful scene",
            ),
            (
                "an anatomical drawing of the heart",
                "an anatomical drawing of the heart",
                "a drawing of a red heart",
            ),
            ("two jade earrings", "two jade earrings", "jade earrings"),
            (
                "a stable without animals",
                "a stable without animals",
                "a stable with animals",
            ),
            ("not a nude figure", "not a nude figure", "a nude figure"),
            (
                "a garden without a fountain",
                "a garden without a fountain",
                "a garden with a fountain",
            ),
        ):
            self.assertEqual(missing_constraints(source, faithful), [])
            self.assertTrue(missing_constraints(source, omitted))
        self.assertTrue(missing_constraints("nude figure", "not nude figure"))
        self.assertTrue(
            missing_constraints("nude figure", "a nude figure, fully dressed")
        )

    def test_owner_live_after_childbirth_regression_2026_10_21(self) -> None:
        observed = (
            "Subjects: Virgin Mary, baby Jesus, Joseph, and various animals.\n"
            "Attributes: Mary and Joseph in biblical attire, newborn Jesus.\n"
            "Scene: Inside a stable in Bethlehem, the moment after childbirth.\n"
            "Style: Photorealistic photograph."
        )
        source = MARY + " ce n'est pas pendant l'accouchement"
        self.assertTrue(missing_constraints(source, observed))
        self.assertIsNone(image_prompt.faithful(source, observed, formatted=True))
        self.assertTrue(
            missing_constraints(
                source, observed.replace("after childbirth", "not during childbirth")
            )
        )

    async def test_stricter_retry_then_literal_then_source(self) -> None:
        translation = "Create a photorealistic photograph of Virgin Mary giving birth to Jesus in the Bethlehem stable, surrounded by animals and Joseph"
        softened = FAITHFUL.replace("giving birth to", "holding").replace(
            "childbirth in progress", "after delivery"
        )
        for retry, expected in (
            (
                json.dumps({"formatted": FAITHFUL, "literal_translation": "unused"}),
                FAITHFUL,
            ),
            (
                json.dumps({"formatted": softened, "literal_translation": translation}),
                translation,
            ),
            (
                json.dumps(
                    {
                        "formatted": softened,
                        "literal_translation": "A mother holding a newborn",
                    }
                ),
                MARY,
            ),
            ("I cannot help.", MARY),
        ):
            budget = image_prompt.RewriteBudget(Decimal("0.05"), Decimal("0.01"))
            with (
                patch.dict(os.environ, {"ESCALATION_MODEL": "local"}),
                patch.object(image_prompt, "reservation", return_value=Decimal("0.01")),
                patch.object(
                    AgentProvider,
                    "complete",
                    AsyncMock(side_effect=[{"text": softened}, {"text": retry}]),
                ) as call,
            ):
                result = await image_prompt.rewrite(MARY, 30, "fr", budget=budget)
            self.assertEqual(result, expected)
            self.assertEqual(call.await_count, 2)
            self.assertEqual(budget.reserved, Decimal("0.02"))
            self.assertIn(
                "literal_translation", str(call.call_args.args[0]["messages"])
            )
            self.assertLess(call.call_args.args[0]["timeout"], 30)

    def test_post_sanitization_check_and_fenced_response(self) -> None:
        self.assertIsNone(
            image_prompt.faithful(
                "nude figure study in charcoal",
                "Subjects: figure\nAttributes: charcoal\nScene: studio\nStyle: figure study",
                formatted=True,
            )
        )
        self.assertEqual(
            image_prompt.candidates(
                '```json\n{"formatted":"ok","literal_translation":"literal"}\n```'
            ),
            ("ok", "literal"),
        )
        self.assertEqual(image_prompt.candidates("{invalid"), ("", ""))
        self.assertIsNone(
            image_prompt.faithful("a garden", "I cannot help", formatted=False)
        )
        self.assertIsNone(
            image_prompt.faithful(
                "nude figure study in charcoal",
                "I cannot create a nude figure study in charcoal",
                formatted=False,
            )
        )
        self.assertIsNone(
            image_prompt.faithful("labels on a box", "labels on a box", formatted=False)
        )

    async def test_configured_role_used_by_transport_and_reservation(self) -> None:
        from serverless import ServerlessPolicy

        for role in ("text", "fast", "code", "vision"):
            with (
                patch.dict(
                    os.environ,
                    {
                        "ATLAS_IMAGE_REWRITE_ROLE": role,
                        "ATLAS_PORT_BASE": os.environ.get("ATLAS_PORT_BASE", "0"),
                        "SCW_GENERATIVE_BASE_URL": "https://provider.invalid",
                        "SCW_GENERATIVE_API_KEY": "test-key",
                    },
                    clear=True,
                ),
                patch.object(AgentProvider, "__init__", return_value=None),
                patch.object(
                    AgentProvider,
                    "_complete",
                    AsyncMock(return_value={"text": FAITHFUL}),
                ) as transport,
            ):
                policy = ServerlessPolicy()
                await image_prompt.ImageRewriteProvider().complete(
                    {
                        "messages": image_prompt.messages(MARY),
                        "tools": [],
                        "local_enabled": False,
                        "timeout": 10,
                    }
                )
                self.assertEqual(transport.call_args.args[2], policy.models[role])
                incoming = len(json.dumps(image_prompt.messages(MARY)).encode()) + 256
                self.assertEqual(
                    image_prompt.reservation(MARY),
                    policy.cost(policy.models[role], incoming, 2048),
                )
                self.assertIsInstance(transport.call_args.args[0], AgentRequest)
        with patch.dict(os.environ, {"ATLAS_IMAGE_REWRITE_ROLE": "unknown"}):
            with self.assertRaisesRegex(ValueError, "invalid_image_rewrite_role"):
                image_prompt.rewrite_role()
