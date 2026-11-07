"""Enforce the owner disposition verbatim and reject public case patches."""

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
import re
import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator.chat_pipeline import _process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Result
from services.orchestrator.model import Configuration, GatewayModel


class ArtifactPromptIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_artifact_prompt_is_absent_when_feature_is_unset(self) -> None:
        artifact_prompt = Path("prompts/artifact.txt").read_text()
        for question in (
            "Please explain why the sky looks blue during the daytime.",
            "Create a small web app.",
        ):
            with self.subTest(question=question):
                prompts = []

                async def runner(
                    query: object,
                    model: object,
                    tools: object,
                    prompt: str,
                    limits: object,
                    **kwargs: object,
                ) -> Result:
                    prompts.append(prompt)
                    return Result(text="Useful answer.", state="done")

                model = GatewayModel(
                    "http://127.0.0.1",
                    Configuration(
                        input_eur_per_mtok=Decimal("0"),
                        output_eur_per_mtok=Decimal("0"),
                        max_tokens=128,
                    ),
                )
                with (
                    patch.object(
                        os,
                        "environ",
                        {
                            key: value
                            for key, value in os.environ.items()
                            if key != "ATLAS_ARTIFACTS"
                        },
                    ),
                    patch.object(
                        GatewayModel, "connect", AsyncMock(return_value=model)
                    ),
                    patch("services.orchestrator.chat_pipeline.run", runner),
                ):
                    await _process(
                        ChatRequest.model_validate(
                            {"messages": [{"role": "user", "content": question}]}
                        ),
                        Interaction(),
                    )
                self.assertEqual(len(prompts), 1)
                self.assertNotIn(artifact_prompt, prompts[0])


class M25FingerprintCoverageTests(unittest.TestCase):
    def test_gate_fails_when_any_non_exempt_prompt_changes(self) -> None:
        from m25_gate import M25_FINGERPRINT_EXEMPT_PROMPTS

        corpus = json.loads(Path("reports/m25-corpus.json").read_text())
        paths = {
            path
            for path in Path("prompts").iterdir()
            if path.is_file() and path not in M25_FINGERPRINT_EXEMPT_PROMPTS
        }
        self.assertTrue(paths)
        actual_hashes = {
            path.as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
        }
        self.assertEqual(corpus["prompt_hashes"], actual_hashes)
        for path in paths:
            with self.subTest(path=path):
                changed_hashes = dict(actual_hashes)
                changed_hashes[path.as_posix()] = hashlib.sha256(
                    path.read_bytes() + b"\nchanged"
                ).hexdigest()
                self.assertNotEqual(changed_hashes, actual_hashes)


class PromptPolicyTests(unittest.TestCase):
    def test_artifact_prompt_is_english_and_self_contained(self) -> None:
        prompt = Path("prompts/artifact.txt").read_text()
        normalized_prompt = " ".join(prompt.split())
        self.assertTrue(prompt.isascii())
        self.assertIn("create_artifact is available", prompt)
        self.assertIn("ONE self-contained HTML file", prompt)
        self.assertIn("No CDN, no external URL", prompt)
        self.assertIn(
            "Never use alert, confirm or prompt; show messages, game over and questions inside the page",
            prompt,
        )
        self.assertIn("Interface language: the user's language", prompt)
        self.assertIn("identifiers and comments stay in English", prompt)
        self.assertIn("If the result is artifact_invalid", prompt)
        self.assertIn(
            'Games end with a clear in-page message (the user-language equivalent of "Game over" with the score) and a visible restart button',
            normalized_prompt,
        )
        self.assertIn(
            "in plain words for a non-technical user: no milliseconds, frame rates or code terms",
            normalized_prompt,
        )

    def test_artifact_prompt_keeps_deliverable_defaults(self) -> None:
        prompt = Path("prompts/artifact.txt").read_text()
        self.assertIn(
            "A requested language, framework or platform is always used", prompt
        )
        self.assertIn("say so in one sentence and give", prompt)
        self.assertIn("the command to run it locally", prompt)

    def test_chat_agent_keeps_code_rules(self) -> None:
        prompt = Path("prompts/chat-agent.txt").read_text()
        self.assertIn("Generated code is always English", prompt)
        self.assertIn("visible labels remain French", prompt)
        self.assertGreater(
            prompt.index("visible labels remain French"),
            prompt.index("Generated code is always English"),
        )

    def test_disposition_and_size(self) -> None:
        owner = Path("contracts/m25-owner.txt").read_text()
        disposition = owner.split("Layer 1, **Disposition**")[1].split("```")[1].strip()
        disposition = disposition.replace(
            "guesses. If you do not know the precedents, search for them.",
            "guesses. If you do not know the precedents, search for them. Give each precedent its date.",
        )
        addition = (
            Path("MISSION.md")
            .read_text()
            .split("Match the answer to the size of the question.", 1)[1]
            .split("Never make a small question large.", 1)[0]
        )
        disposition = disposition.replace(
            "Give each precedent its date.\n\n",
            "Give each precedent its date.\n\n"
            + "Match the answer to the size of the question."
            + addition
            + "Never make a small question large.\n\n",
        )
        prompt = Path("prompts/chat.txt").read_text()
        self.assertTrue(prompt.startswith(disposition + "\n"))
        self.assertLess(len(prompt.encode()), 3500)
        layers = ["Capabilities and tools", "Language and form", "Safety and evidence"]
        positions = [prompt.index(layer) for layer in layers]
        self.assertEqual(positions, sorted(positions))

    def test_recalled_sources_cannot_claim_retrieval(self) -> None:
        prompt = Path("prompts/chat.txt").read_text()
        self.assertIn(
            "consultation dates only for sources retrieved during this answer", prompt
        )
        self.assertIn(
            "training-memory sources as recalled, without dates, URLs", prompt
        )
        self.assertIn("Never claim a search or reading without a tool call", prompt)

    def test_supplied_scenario_premises_are_distinct_from_external_facts(self) -> None:
        prompt = Path("prompts/web-chat.txt").read_text()
        self.assertIn("Use explicit scenario assumptions as premises", prompt)
        self.assertIn("Research only missing external facts", prompt)

    def test_no_case_vocabulary_in_any_prompt(self) -> None:
        gate = Path("scripts/verify-m25.sh").read_text()
        terms = gate.split("<< 'EOF'\n", 1)[1].split("\nEOF", 1)[0].splitlines()
        for path in (p for p in Path("prompts").iterdir() if p.is_file()):
            for term in terms:
                with self.subTest(path=path, term=term):
                    self.assertIsNone(
                        re.search(
                            r"\b" + re.escape(term) + r"\b", path.read_text(), re.I
                        )
                    )


class SourceAvailabilityTests(unittest.TestCase):
    def test_analysis_does_not_manufacture_its_source(self) -> None:
        prompt = Path("prompts/files.txt").read_text()
        self.assertIn("Never create a substitute source to analyze", prompt)
        self.assertIn("ask for the missing input", prompt)
        self.assertIn("explicitly asks you to create", prompt)
