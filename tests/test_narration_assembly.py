"""Pre-answer synthesis narration is stripped without changing useful answers."""

import unittest
from services.orchestrator.narration import NarrationFilter, clean_answer


class SynthesisNarrationTests(unittest.TestCase):
    def test_synthesis_preamble_across_every_chunk_boundary(self) -> None:
        for prefix in (
            "J'ai maintenant suffisamment d'éléments pour synthétiser la réponse.",
            "J’ai maintenant suffisamment d’éléments pour synthétiser la réponse.",
            "J'ai maintenant suffisamment d'elements pour synthetiser la reponse.",
        ):
            source = prefix + " The fictional total is 42."
            self.assertEqual(clean_answer(source), "The fictional total is 42.")
            for size in range(1, len(source)):
                parser = NarrationFilter()
                answer = "".join(
                    parser.feed(source[i : i + size])
                    for i in range(0, len(source), size)
                )
                self.assertEqual(
                    (answer + parser.finish()).strip(), "The fictional total is 42."
                )

    def test_substantive_and_quoted_statements_survive(self) -> None:
        for source in (
            "J'ai maintenant suffisamment de fonds pour acheter ce livre.",
            "> J'ai maintenant suffisamment d'éléments pour synthétiser.",
            "```text\nJ'ai maintenant suffisamment d'éléments pour synthétiser.\n```",
        ):
            self.assertEqual(clean_answer(source), source)
