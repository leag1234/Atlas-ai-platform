"""Independent paired grading statistics for POC-R2; no provider I/O."""

from collections import Counter
from typing import Any

LANGUAGES = frozenset({"fr", "de", "es", "it", "en"})


def agreement(left: list[int], right: list[int]) -> dict[str, float]:
    if not left or len(left) != len(right):
        raise ValueError("unpaired_grades")
    if any(type(n) is not int or not 1 <= n <= 5 for n in left + right):
        raise ValueError("invalid_grade")
    size = len(left)
    a, b = Counter(left), Counter(right)
    observed = sum(x == y for x, y in zip(left, right, strict=True)) / size
    expected = sum(a[n] * b[n] for n in range(1, 6)) / size**2
    if expected == 1:
        raise ValueError("undefined_kappa")
    return {
        "kappa": (observed - expected) / (1 - expected),
        "agreement": observed,
        "mean_absolute_difference": sum(
            abs(x - y) for x, y in zip(left, right, strict=True)
        )
        / size,
    }


def calibrate(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    if (
        len(pairs) != 30
        or len({p["id"] for p in pairs}) != 30
        or Counter(p["lang"] for p in pairs) != dict.fromkeys(LANGUAGES, 6)
    ):
        raise ValueError("invalid_calibration_sample")
    return {
        "type": "calibration croisée inter-modèles",
        "sample_size": len(pairs),
        **agreement([p["production"] for p in pairs], [p["reference"] for p in pairs]),
        "par_langue": {
            lang: agreement(
                [p["production"] for p in pairs if p["lang"] == lang],
                [p["reference"] for p in pairs if p["lang"] == lang],
            )
            for lang in sorted(LANGUAGES)
        },
        "pairs": pairs,
    }


def best_effort(
    pairs: list[dict[str, Any]], excluded: list[dict[str, str]]
) -> dict[str, Any]:
    """MISSION PoC: report exclusions; an undefined subgroup is never perfect."""
    ids = [p["id"] for p in pairs + excluded]
    if len(set(ids)) != len(ids) or any(p["lang"] not in LANGUAGES for p in pairs):
        raise ValueError("invalid_calibration_sample")
    global_scores = agreement(
        [p["production"] for p in pairs], [p["reference"] for p in pairs]
    )
    languages: dict[str, Any] = {}
    for lang in sorted(LANGUAGES):
        group = [p for p in pairs if p["lang"] == lang]
        try:
            scores: dict[str, Any] = agreement(
                [p["production"] for p in group], [p["reference"] for p in group]
            )
        except ValueError as error:
            if str(error) not in {"undefined_kappa", "unpaired_grades"}:
                raise
            scores = {"kappa": None, "reason": str(error)}
        languages[lang] = scores | {"sample_size": len(group)}
    return {
        "type": "calibration croisée inter-modèles (PoC best-effort)",
        "sample_size": len(pairs),
        **global_scores,
        "par_langue": languages,
        "pairs": pairs,
        "excluded": excluded,
    }
