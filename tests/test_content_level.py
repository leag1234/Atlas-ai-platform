"""Administrator content level: attestation, routing, and strict limits."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from packages.content_policy import (
    extended_instruction,
    extended_profile,
    extended_request,
    text_refusal,
)
from packages.image_policy import ImagePolicy, load_policy, tighten
from packages.imagegen import ImagePrompt

FIXTURE_PATTERN = r"\bfixture-extended-request\b"
FIXTURE_INSTRUCTION = "FIXTURE EXTENDED INSTRUCTION"
EXTENDED = ImagePolicy(
    content_level="extended",
    adult_audience_attested=True,
    extended_request_patterns={"en": [FIXTURE_PATTERN]},
    extended_instruction=FIXTURE_INSTRUCTION,
    extended_profiles=["atlas-deepseek", "atlas-glm"],
)
STANDARD = ImagePolicy(
    extended_request_patterns={"en": [FIXTURE_PATTERN]},
    extended_instruction=FIXTURE_INSTRUCTION,
)


def _load(content: object) -> ImagePolicy:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory, "policy.json")
        path.write_text(json.dumps(content))
        return load_policy(path)


class PolicyFileTests(unittest.TestCase):
    def test_extended_requires_attestation_and_accepts_legacy_alias(self) -> None:
        content = {
            "content_level": "extended",
            "adult_audience_attested": True,
            "extended_request_patterns": {"en": [FIXTURE_PATTERN]},
            "extended_instruction": FIXTURE_INSTRUCTION,
            "extended_profiles": ["atlas-deepseek"],
        }
        self.assertEqual(
            _load(content),
            EXTENDED.model_copy(update={"extended_profiles": ["atlas-deepseek"]}),
        )
        self.assertEqual(
            _load({**content, "content_level": "adult"}).content_level, "extended"
        )
        invalids: tuple[object, ...] = (
            {"content_level": "extended"},
            {**content, "adult_audience_attested": False},
            {**content, "image_safety_level": "strict"},
            {**content, "extended_request_patterns": {"en": ["["]}},
            {**content, "extended_request_patterns": {"en": [42]}},
            {**content, "extended_instruction": [FIXTURE_INSTRUCTION]},
            {"content_level": "everything"},
        )
        for invalid in invalids:
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                _load(invalid)

    def test_organizations_only_tighten(self) -> None:
        self.assertEqual(tighten(EXTENDED, STANDARD).content_level, "standard")
        self.assertEqual(tighten(EXTENDED, EXTENDED).content_level, "extended")
        self.assertEqual(tighten(STANDARD, EXTENDED).content_level, "standard")
        strict = ImagePolicy(image_safety_level="strict")
        self.assertEqual(tighten(EXTENDED, strict).content_level, "standard")


class ImagePromptTests(unittest.TestCase):
    def test_configured_request_is_only_allowed_at_extended_level(self) -> None:
        prompt = "fixture-extended-request"
        with patch("packages.image_limits.POLICY", STANDARD):
            with self.assertRaises(ValueError):
                ImagePrompt(prompt=prompt)
        with patch("packages.image_limits.POLICY", EXTENDED):
            self.assertEqual(ImagePrompt(prompt=prompt).prompt, prompt)

    def test_hard_limit_precedes_extended_request_handling(self) -> None:
        with (
            patch("packages.image_limits.POLICY", EXTENDED),
            patch("packages.imagegen.image_limit", return_value="minors"),
            patch("packages.imagegen.extended_request", return_value=True),
        ):
            with self.assertRaisesRegex(ValueError, "image_content_refused:minors"):
                ImagePrompt(prompt="fixture-extended-request")


class TextPolicyTests(unittest.TestCase):
    def test_configured_request_and_instruction(self) -> None:
        with (
            patch("packages.image_limits.POLICY", EXTENDED),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertTrue(extended_request("fixture-extended-request"))
            self.assertFalse(extended_request("ordinary request"))
            self.assertEqual(extended_instruction(), FIXTURE_INSTRUCTION)
            self.assertEqual(extended_profile(), "atlas-deepseek")
        self.assertTrue(text_refusal("minors", "fr").startswith("Je n’écris pas"))

    def test_incomplete_extended_policy_fails_closed(self) -> None:
        incomplete = ImagePolicy(content_level="extended", adult_audience_attested=True)
        with patch("packages.image_limits.POLICY", incomplete):
            self.assertEqual(extended_instruction(), "")
            self.assertFalse(extended_request("fixture-extended-request"))

    def test_pipeline_routes_configured_requests_to_profile(self) -> None:
        from services.orchestrator import chat_pipeline
        from services.orchestrator.chat_schema import ChatMessage, ChatRequest
        from services.orchestrator.interactions import Interaction

        def run(text: str, policy: ImagePolicy) -> tuple[AsyncMock, Interaction]:
            inner = AsyncMock()
            item = Interaction()
            with (
                patch("packages.image_limits.POLICY", policy),
                patch.dict(os.environ, {}, clear=True),
                patch.object(chat_pipeline, "_process", inner),
            ):
                import asyncio

                asyncio.run(
                    chat_pipeline.process(
                        ChatRequest(
                            messages=[ChatMessage(role="user", content=text)],
                            model="atlas-qwen",
                            lang="fr",
                        ),
                        item,
                    )
                )
            return inner, item

        inner, _ = run("fixture-extended-request", EXTENDED)
        self.assertEqual(inner.await_args_list[0].args[0].model, "atlas-deepseek")
        inner, _ = run("fixture-extended-request", STANDARD)
        self.assertEqual(inner.call_args.args[0].model, "atlas-qwen")
        with patch(
            "services.orchestrator.chat_pipeline.text_limit", return_value="minors"
        ):
            inner, item = run("fixture-extended-request", EXTENDED)
        inner.assert_not_awaited()
        self.assertTrue(item.reponse.startswith("Je n’écris pas"))

    def test_environment_profile_aliases(self) -> None:
        with patch("packages.image_limits.POLICY", EXTENDED):
            with patch.dict(
                os.environ, {"ATLAS_EXTENDED_PROFILES": "atlas-glm"}, clear=False
            ):
                self.assertEqual(extended_profile(), "atlas-glm")
            with patch.dict(
                os.environ, {"ATLAS_ADULT_PROFILES": "atlas-glm"}, clear=True
            ):
                self.assertEqual(extended_profile(), "atlas-glm")
            with patch.dict(
                os.environ, {"ATLAS_ADULT_PROFILE": "atlas-glm"}, clear=True
            ):
                self.assertEqual(extended_profile(), "atlas-glm")

    def test_routed_profile_fallback_empty_then_success(self) -> None:
        self._assert_routed_profile_fallback([("", 0.01), ("complete", 0.02)])

    def test_routed_profile_fallback_refusal_then_success(self) -> None:
        self._assert_routed_profile_fallback(
            [("I cannot help with that.", 0.01), ("complete", 0.02)]
        )

    def test_routed_profile_fallback_success_does_not_retry(self) -> None:
        calls = self._assert_routed_profile_fallback([("complete", 0.01)])
        self.assertEqual(calls, ["atlas-deepseek"])

    def test_routed_profile_fallback_returns_last_attempt(self) -> None:
        calls = self._assert_routed_profile_fallback(
            [("I cannot help with that.", 0.01), ("No puedo ayudar.", 0.02)]
        )
        self.assertEqual(calls, ["atlas-deepseek", "atlas-glm"])

    def test_routed_profile_fallback_stops_when_budget_is_spent(self) -> None:
        calls = self._assert_routed_profile_fallback(
            [("I cannot help with that.", 0.10)]
        )
        self.assertEqual(calls, ["atlas-deepseek"])

    def test_routed_profile_fallback_checks_hard_limit_before_call(self) -> None:
        from services.orchestrator import chat_pipeline
        from services.orchestrator.chat_schema import ChatMessage, ChatRequest
        from services.orchestrator.interactions import Interaction

        inner = AsyncMock()
        with (
            patch("packages.image_limits.POLICY", EXTENDED),
            patch(
                "services.orchestrator.chat_pipeline.text_limit", return_value="minors"
            ),
            patch.object(chat_pipeline, "_process", inner),
        ):
            import asyncio

            asyncio.run(
                chat_pipeline.process(
                    ChatRequest(
                        messages=[
                            ChatMessage(role="user", content="fixture-extended-request")
                        ]
                    ),
                    Interaction(),
                )
            )
        inner.assert_not_awaited()

    def test_routed_profile_fallback_buffers_first_short_result(self) -> None:
        from services.orchestrator import chat_pipeline
        from services.orchestrator.chat_schema import ChatMessage, ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.stream_client import sink_context

        events: list[dict[str, object]] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        async def scripted(request: object, item: Interaction, **_: object) -> None:
            if getattr(request, "model") == "atlas-deepseek":
                active_sink = sink_context.get()
                assert active_sink is not None
                await active_sink({"delta": {"content": "I cannot help with that."}})
                item.reponse, item.cout_eur = "I cannot help with that.", 0.01
            else:
                item.reponse, item.cout_eur = "complete", 0.02

        token = sink_context.set(sink)
        try:
            with (
                patch("packages.image_limits.POLICY", EXTENDED),
                patch.dict(os.environ, {}, clear=True),
                patch.object(chat_pipeline, "_process", new=scripted),
            ):
                import asyncio

                asyncio.run(
                    chat_pipeline.process(
                        ChatRequest(
                            messages=[
                                ChatMessage(
                                    role="user", content="fixture-extended-request"
                                )
                            ]
                        ),
                        Interaction(),
                    )
                )
        finally:
            sink_context.reset(token)
        self.assertFalse(any("cannot" in str(event) for event in events))

    def test_routed_profile_fallback_leaves_other_requests_unchanged(self) -> None:
        self._assert_routed_profile_fallback([("complete", 0.01)], text="hello")

    def _assert_routed_profile_fallback(
        self, outcomes: list[tuple[str, float]], text: str = "fixture-extended-request"
    ) -> list[str]:
        from services.orchestrator import chat_pipeline
        from services.orchestrator.chat_schema import ChatMessage, ChatRequest
        from services.orchestrator.interactions import Interaction

        calls: list[str] = []

        async def scripted(request: object, item: Interaction, **_: object) -> None:
            calls.append(getattr(request, "model"))
            answer, cost = outcomes[len(calls) - 1]
            item.reponse, item.cout_eur, item.state = answer, cost, "done"

        with (
            patch("packages.image_limits.POLICY", EXTENDED),
            patch.dict(os.environ, {}, clear=True),
            patch.object(chat_pipeline, "_process", new=scripted),
        ):
            import asyncio

            asyncio.run(
                chat_pipeline.process(
                    ChatRequest(messages=[ChatMessage(role="user", content=text)]),
                    Interaction(),
                )
            )
        return calls


if __name__ == "__main__":
    unittest.main()
