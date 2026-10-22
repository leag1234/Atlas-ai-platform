"""Enforce the owner disposition verbatim and reject public case patches."""

from pathlib import Path
import re
import unittest


class PromptPolicyTests(unittest.TestCase):
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
