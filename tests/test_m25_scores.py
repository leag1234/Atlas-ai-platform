"""Keep the owner's two exceptions narrow; all other score failures still block."""

import unittest
from typing import Any

from m25_scores import corpus_means


class PublicScoreTests(unittest.TestCase):
    def cases(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "C03-expert-derivation" if i == 3 else f"C{i:02}",
                "scores": {
                    p: {"score": s} for p, s in zip(self.profiles, (6, 5.1, 4.8))
                },
                "meta": {p: {"error": None} for p in self.profiles},
            }
            for i in range(15)
        ]

    profiles = ("atlas-qwen", "atlas-glm", "atlas-deepseek")

    def test_owner_floor_and_only_permitted_unavailable_case(self) -> None:
        cases = self.cases()
        cases[3]["scores"]["atlas-glm"]["score"] = None
        cases[3]["meta"]["atlas-glm"]["error"] = "HTTP Error 504"
        means = corpus_means(cases)
        self.assertAlmostEqual(means[2], 4.8)
        for index, profile in ((2, "atlas-glm"), (3, "atlas-qwen")):
            with self.subTest(index=index, profile=profile):
                invalid = self.cases()
                invalid[index]["scores"][profile]["score"] = None
                invalid[index]["meta"][profile]["error"] = "HTTP Error 504"
                with self.assertRaises(AssertionError):
                    corpus_means(invalid)

    def test_other_provider_failures_are_not_exempt(self) -> None:
        cases = self.cases()
        cases[3]["scores"]["atlas-glm"]["score"] = None
        cases[3]["meta"]["atlas-glm"]["error"] = "HTTP Error 401"
        with self.assertRaises(AssertionError):
            corpus_means(cases)

    def test_lower_scores_missing_scores_and_duplicate_cases_block(self) -> None:
        for profile in self.profiles[:2]:
            cases = self.cases()
            cases[0]["scores"][profile]["score"] -= 0.01
            with self.assertRaises(AssertionError):
                corpus_means(cases)
        cases = self.cases()
        cases[3]["scores"]["atlas-glm"]["score"] = None
        with self.assertRaises(AssertionError):
            corpus_means(cases)

    def test_owner_certifies_two_profiles_but_keeps_degraded_measurement(self) -> None:
        cases = self.cases()
        for case in cases:
            case["scores"]["atlas-deepseek"]["score"] = 0
        self.assertEqual(corpus_means(cases)[2], 0)
        for invalid in (None, -1, 11, float("nan")):
            cases[0]["scores"]["atlas-deepseek"]["score"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                corpus_means(cases)
        cases = self.cases()
        cases[0] = cases[1]
        with self.assertRaises(AssertionError):
            corpus_means(cases)
