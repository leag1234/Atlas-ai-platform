"""Sampling options of the OpenAI and Anthropic APIs reach the provider on the direct models."""

import json
import os
import unittest
from typing import Any
from unittest.mock import patch

from pydantic import ValidationError

import dev_gateway
from packages.api_content import drop_client_extras, unsupported_option
from packages.dev_request import Request
from services.orchestrator.dev_chat import RawChatInput
from services.orchestrator.dev_input import messages as anthropic_messages

KEYS = {"MISTRAL_API_KEY": "test-only", "SCW_GENERATIVE_API_KEY": "test-only"}


def raw(**options: Any) -> dict[str, Any]:
    return {
        "model": "raw/public-routing",
        "messages": [{"role": "user", "content": "Reply OK"}],
        "max_tokens": 20,
        **options,
    }


def plans(request: Any) -> list[dict[str, Any]]:
    with patch.dict(os.environ, KEYS):
        plan = dev_gateway.prepare_api(request, "atlas-code", "key-test")
    bodies = []
    current: dev_gateway.Plan | None = plan
    while current is not None:
        bodies.append(dict(json.loads(current.body), provider=current.provider))
        current = current.fallback
    return bodies


class SamplingTests(unittest.TestCase):
    def test_sampling_options_reach_every_provider(self) -> None:
        options = {
            "top_p": 0.9,
            "stop": ["\n\n"],
            "seed": 7,
            "frequency_penalty": 0.2,
            "presence_penalty": 0.1,
        }
        for body in plans(RawChatInput.model_validate(raw(**options))):
            with self.subTest(provider=body["provider"]):
                self.assertEqual(body["top_p"], 0.9)
                self.assertEqual(body["stop"], ["\n\n"])
                self.assertEqual(body["frequency_penalty"], 0.2)
                self.assertEqual(body["presence_penalty"], 0.1)
                # Mistral names the seed random_seed; the others keep seed.
                seed = "random_seed" if body["provider"] == "mistral" else "seed"
                self.assertEqual(body[seed], 7)
                self.assertNotIn(
                    "seed" if seed == "random_seed" else "random_seed", body
                )

    def test_unset_options_are_not_sent(self) -> None:
        for body in plans(RawChatInput.model_validate(raw())):
            for name in ("top_p", "stop", "seed", "random_seed", "top_k", "n"):
                self.assertNotIn(name, body)

    def test_max_completion_tokens_is_max_tokens(self) -> None:
        body = raw(max_completion_tokens=33)
        body.pop("max_tokens")
        self.assertEqual(RawChatInput.model_validate(body).max_tokens, 33)
        with self.assertRaises(ValidationError):
            RawChatInput.model_validate(raw(max_completion_tokens=33))

    def test_out_of_range_and_several_choices_are_refused(self) -> None:
        for options in (
            {"top_p": 1.5},
            {"stop": ["a"] * 5},
            {"n": 2},
            {"frequency_penalty": 3},
        ):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                RawChatInput.model_validate(raw(**options))
        self.assertEqual(RawChatInput.model_validate(raw(n=1)).max_tokens, 20)

    def test_options_no_provider_applies_are_refused_by_name(self) -> None:
        self.assertEqual(unsupported_option(raw(logit_bias={"1": -1})), "logit_bias")
        self.assertEqual(unsupported_option(raw(logprobs=True)), "logprobs")
        self.assertIsNone(unsupported_option(raw(logprobs=False, logit_bias={})))
        # Harmless options without effect are still dropped, not refused.
        body = drop_client_extras(raw(store=False, user="u", service_tier="auto"))
        self.assertEqual(RawChatInput.model_validate(body).max_tokens, 20)

    def test_anthropic_sampling_names(self) -> None:
        converted = anthropic_messages(
            {
                "model": "atlas-code",
                "max_tokens": 20,
                "messages": [{"role": "user", "content": "Reply OK"}],
                "stop_sequences": ["END"],
                "top_p": 0.8,
                "top_k": 40,
                "service_tier": "auto",
            }
        )
        self.assertEqual(
            (converted.stop, converted.top_p, converted.top_k), (["END"], 0.8, 40)
        )

    def test_code_gateway_request_accepts_the_same_options(self) -> None:
        request = Request.model_validate(
            {
                "messages": [{"role": "user", "content": "Reply OK"}],
                "top_p": 0.5,
                "stop": "END",
            }
        )
        self.assertEqual((request.top_p, request.stop), (0.5, "END"))
