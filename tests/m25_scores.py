"""Public development thresholds, including explicit owner exceptions of 2026-09-21."""

from typing import Any
import re


def corpus_means(cases: list[dict[str, Any]]) -> list[float]:
    assert len(cases) == 15
    assert len({case["id"] for case in cases}) == 15
    means = []
    for profile, floor in (
        ("atlas-qwen", 6.0),
        ("atlas-glm", 5.1),
        ("atlas-deepseek", None),
    ):
        scores = []
        for case in cases:
            score = case["scores"].get(profile, {}).get("score")
            error = case["meta"][profile]["error"]
            if (
                case["id"] == "C03-expert-derivation"
                and profile == "atlas-glm"
                and error
            ):
                assert re.search(r"HTTP Error 50[234]\b", str(error)), error
                assert score is None, "failed_C03_must_not_have_a_score"
                continue  # Owner permits not-run, never an invented zero or success.
            assert not error, (case["id"], profile, error)
            assert type(score) in (int, float) and 0 <= score <= 10
            scores.append(score)
        mean = sum(scores) / len(scores)
        # MISSION's final owner ruling certifies Qwen/GLM only. The third
        # profile still needs complete, valid measurements and is reported.
        if floor is not None:
            assert mean >= floor, (profile, mean, floor)
        means.append(mean)
    return means
