"""Configured raw-model IDs and pricing are enforced before provider I/O."""

from decimal import Decimal
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import dev_gateway
from packages.limits import LimitError
from services.orchestrator.dev_chat import RawChatInput


class RawModelTests(unittest.TestCase):
    def policy(
        self, *, unpriced: bool = False, missing_price: bool = False
    ) -> SimpleNamespace:
        model = "fictional-vision-chat"
        return SimpleNamespace(
            models={
                "text": "fictional-text-chat",
                "vision": model,
                "embedding": "fictional-embedding",
            },
            config={
                "text": {"capabilities": {"fictional-text-chat": {"context": 32768}}},
                "vision": {"capabilities": {model: {"context": 32768, "vision": True}}},
                "embedding": {
                    "capabilities": {"fictional-embedding": {"dimensions": 8}}
                },
            },
            prices=(
                {
                    "fictional-text-chat": {
                        "input_eur_per_mtok": "1",
                        "output_eur_per_mtok": "2",
                    },
                    "fictional-embedding": {
                        "input_eur_per_mtok": "1",
                        "output_eur_per_mtok": "0",
                    },
                }
                if missing_price
                else {
                    "fictional-text-chat": {
                        "input_eur_per_mtok": "1",
                        "output_eur_per_mtok": "2",
                    },
                    model: {
                        "input_eur_per_mtok": "1",
                        "output_eur_per_mtok": "2",
                        "unpriced": unpriced,
                    },
                    "fictional-embedding": {
                        "input_eur_per_mtok": "1",
                        "output_eur_per_mtok": "0",
                    },
                }
            ),
        )

    def request(self) -> RawChatInput:
        return RawChatInput.model_validate(
            {
                "model": "raw/fictional-vision-chat",
                "max_tokens": 10,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "https://example.invalid/fictional.png"
                                },
                            }
                        ],
                    }
                ],
            }
        )

    def test_chat_roles_are_listed_and_embeddings_are_not(self) -> None:
        with patch.object(dev_gateway, "ServerlessPolicy", return_value=self.policy()):
            self.assertEqual(
                dev_gateway.raw_models(),
                {
                    "raw/fictional-text-chat": "text",
                    "raw/fictional-vision-chat": "vision",
                },
            )

    def test_priced_model_reserves_its_selected_rate(self) -> None:
        with (
            patch.object(dev_gateway, "ServerlessPolicy", return_value=self.policy()),
            patch.dict(
                os.environ,
                {"SCW_GENERATIVE_BASE_URL": "https://provider.invalid/v1"},
                clear=False,
            ),
        ):
            plan = dev_gateway.prepare_raw(self.request())
        self.assertGreater(plan.reserved, 0)
        self.assertEqual(plan.input_rate, Decimal("1"))
        self.assertIn("fictional-vision-chat", plan.body)

    def test_image_input_requires_a_vision_capability(self) -> None:
        policy = self.policy()
        policy.models["text"] = "fictional-text-chat"
        with (
            patch.object(dev_gateway, "ServerlessPolicy", return_value=policy),
            patch.dict(
                os.environ,
                {"SCW_GENERATIVE_BASE_URL": "https://provider.invalid/v1"},
                clear=False,
            ),
        ):
            request = self.request().model_copy(
                update={"model": "raw/fictional-text-chat"}
            )
            with self.assertRaisesRegex(ValueError, "missing_capabilities"):
                dev_gateway.prepare_raw(request)

    def test_unpriced_model_is_refused_before_transport(self) -> None:
        with (
            patch.object(
                dev_gateway, "ServerlessPolicy", return_value=self.policy(unpriced=True)
            ),
            self.assertRaisesRegex(LimitError, "unpriced_model"),
        ):
            dev_gateway.prepare_raw(self.request())

    def test_missing_price_is_refused_before_transport(self) -> None:
        with (
            patch.object(
                dev_gateway,
                "ServerlessPolicy",
                return_value=self.policy(missing_price=True),
            ),
            self.assertRaisesRegex(LimitError, "unpriced_model"),
        ):
            dev_gateway.prepare_raw(self.request())
