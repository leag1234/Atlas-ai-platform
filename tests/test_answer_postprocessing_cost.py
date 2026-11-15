"""Answer post-processing cost on degenerate answers (staging freeze 2026-11-15).

Every detector that runs on a delivered answer holds the interpreter while it
runs: a super-linear regular expression on a long answer freezes every endpoint.
Each detector here is run on long degenerate answers that make such expressions
backtrack or rescan (space runs, unclosed brackets and tags, long sums, digit
runs). A new expression on this path that is not linear fails here.
"""

from collections.abc import Callable
import time
import unittest

from services.orchestrator.citation_cleanup import cleanup
from services.orchestrator.citation_fragments import tool_citations
from services.orchestrator.file_results import canonical_links
from services.orchestrator.grounding import extract_claims
from services.orchestrator.image_truth import verified_images
from services.orchestrator.narration import ProtocolFilter, clean_answer
from services.orchestrator.number_provenance import Provenance
from services.orchestrator.project_citations import render as project_citations
from services.orchestrator.source_truth import (
    clean_citation_residue,
    inline_source_markers,
    person_citations,
    without_unretrieved,
)

RETRIEVED = "https://example.test/page"

# About 100 000 characters each: a long answer, well within one model turn.
ANSWERS = {
    "space run": " " * 100_000 + "x",
    "tab run before a phrase": "\t" * 50_000 + "according to sources.",
    "space runs before a stop": ("word" + " " * 5_000 + "x") * 20,
    "unclosed brackets": "[" * 100_000,
    "unclosed links": "[a](" * 25_000,
    "unclosed details": "<details>" * 10_000,
    "unclosed images": "![" * 50_000,
    "backticks": "`" * 100_000,
    "inline code": "`a" * 50_000,
    "fences": "```" * 30_000,
    "fence lines": "```\n" * 25_000,
    "open object": "{" + "a" * 100_000,
    "reference run": "[[atlas-reference:1]], " + "a" * 100_000,
    "digit run": "1" * 100_000 + "x",
    "sum without result": "Total : " + " + ".join(["12,50"] * 12_000),
    "space-grouped digits": "1 234 " * 15_000 + "5x",
    "links": f"[a]({RETRIEVED}) " * 3_000,
    "urls": "https://example.test/a" * 4_000,
    "slash label": "1. word" + " " * 50_000 + "/",
    "rules": "---\n" * 25_000,
    "numbered titles": "1. Title / Other\n" * 6_000,
    "words": "word " * 20_000,
    "dots": "a." * 50_000,
    "sentences": "Valmorel had 12 arches in 1890. " * 3_000,
}


def protocol(text: str) -> str:
    held = ProtocolFilter()
    return held.feed(text) + held.finish()


DETECTORS: dict[str, Callable[[str], object]] = {
    "clean_citation_residue": clean_citation_residue,
    "without_unretrieved": lambda text: without_unretrieved(text, {RETRIEVED}),
    "inline_source_markers": lambda text: inline_source_markers(text, [RETRIEVED]),
    "tool_citations": lambda text: tool_citations(text, {RETRIEVED}),
    "citation cleanup": cleanup,
    "canonical_links": lambda text: canonical_links(text, ["/files/report.pdf"]),
    "person_citations": lambda text: person_citations(text, {RETRIEVED}),
    "project citations": lambda text: project_citations(text, [], "org", "project"),
    "clean_answer": clean_answer,
    "protocol filter": lambda text: protocol(text),
    "verified_images": lambda text: verified_images(text, set()),
    "extract_claims": extract_claims,
    "number provenance": lambda text: Provenance().report(text, [text[:2_000]]),
}

# Linear work on 100 000 characters takes milliseconds; the budget leaves room
# for a slow CI machine, and a quadratic scan of these answers takes far longer.
BUDGET_SECONDS = 1.0


class AnswerPostprocessingCostTests(unittest.TestCase):
    def test_detectors_stay_within_budget_on_degenerate_answers(self) -> None:
        slow = []
        for detector, run in DETECTORS.items():
            for name, answer in ANSWERS.items():
                start = time.perf_counter()
                run(answer)
                seconds = time.perf_counter() - start
                if seconds > BUDGET_SECONDS:
                    slow.append(f"{detector} on {name}: {seconds:.2f} s")
        self.assertEqual(slow, [])


if __name__ == "__main__":
    unittest.main()
