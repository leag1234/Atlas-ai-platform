"""POC-R1/F8: strict score aggregation and durable, escaped reports."""

import csv
from contextlib import closing
import html
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

THRESHOLDS = dict(
    e1=0.7, e2=0.8, e3=1.0, e4=0.9, e5=0.9, e6=0.0, e7=0.8, e8=0.85, e9=0.8
)


def aggregate(rows: list[dict[str, Any]], full: bool = True) -> dict[str, Any]:
    if not rows or len({r["id"] for r in rows}) != len(rows):
        raise ValueError("empty_or_duplicate_results")
    suites = {r["suite"] for r in rows}
    if (full and suites != set(THRESHOLDS)) or not suites <= set(THRESHOLDS):
        raise ValueError("missing_or_unknown_suite")
    if any(not math.isfinite(r["score"]) or not 0 <= r["score"] <= 1 for r in rows):
        raise ValueError("invalid_score")
    results = {}
    for suite in sorted(suites):
        group = [r for r in rows if r["suite"] == suite]
        languages = {
            lang: mean(r["score"] for r in group if r["lang"] == lang)
            for lang in sorted({r["lang"] for r in group})
        }
        score = mean(r["score"] for r in group)
        spread = (
            (max(languages.values()) - min(languages.values()))
            / max(languages.values())
            if max(languages.values())
            else 0.0
        )
        results[suite] = dict(
            score=score,
            count=len(group),
            threshold=THRESHOLDS[suite],
            par_langue=languages,
            language_gap=spread,
            poc_passed=score >= THRESHOLDS[suite]
            and not any(r.get("critical_failure", False) for r in group),
            passed=score >= THRESHOLDS[suite]
            and not any(r.get("critical_failure", False) for r in group)
            and (suite not in {"e2", "e6", "e7", "e9"} or spread <= 0.15),
        )
    return dict(
        suites=results,
        par_langue={
            lang: {
                s: v["par_langue"][lang]
                for s, v in results.items()
                if lang in v["par_langue"]
            }
            for lang in sorted({r["lang"] for r in rows})
        },
        quality_go=all(v["passed"] for v in results.values()),
        poc_passed=all(v["poc_passed"] for v in results.values()),
        cases=rows,
    )


def publish(
    root: Path,
    report: dict[str, Any],
    telemetry: list[dict[str, Any]],
    calibration: dict[str, Any],
) -> None:
    if not telemetry or any(
        not math.isfinite(r[k]) or r[k] < 0
        for r in telemetry
        for k in ("tokens", "cost", "latency", "ttft")
    ):
        raise ValueError("invalid_telemetry")
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(root / "runs.sqlite")) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS runs (stamp TEXT PRIMARY KEY, report TEXT NOT NULL)"
        )
        previous = db.execute(
            "SELECT report FROM runs ORDER BY stamp DESC LIMIT 1"
        ).fetchone()
        old = json.loads(previous[0]) if previous else {}
        report = report | {
            "created_at": stamp,
            "diff": {
                s: v["score"] - old["suites"][s]["score"]
                for s, v in report["suites"].items()
                if s in old.get("suites", {})
            },
        }
        db.execute(
            "INSERT INTO runs VALUES (?,?)",
            (stamp, json.dumps(report, allow_nan=False)),
        )
    summary = {k: sum(r[k] for r in telemetry) for k in ("tokens", "cost")}
    for key in ("latency", "ttft", "tok_s", "cache_hit_ratio"):
        observed = [r[key] for r in telemetry if r.get(key) is not None]
        summary[key] = mean(observed) if observed else None
        summary[key + "_coverage"] = len(observed) / len(telemetry)
    summary["requests"] = telemetry
    for name, value in [
        ("report", report),
        ("telemetry", summary),
        ("calibration", calibration),
    ]:
        (root / (name + ".json")).write_text(
            json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        )
    columns = sorted({k for row in telemetry for k in row})
    with (root / "telemetry.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(telemetry)
    document = '<!doctype html><html lang="fr"><meta charset="utf-8"><title>ATLAS — évaluation</title><h1>Évaluation ATLAS</h1>'
    for title, value in [
        (
            "Coût et latence (EUR, secondes)",
            {k: v for k, v in summary.items() if k != "requests"},
        ),
        ("Calibration croisée inter-modèles", calibration),
        ("Suites, langues et diff précédent", report),
    ]:
        document += (
            "<h2>"
            + title
            + "</h2><pre>"
            + html.escape(json.dumps(value, ensure_ascii=False, indent=2))
            + "</pre>"
        )
    document += "</html>"
    (root / "report.html").write_text(document)
    (root / ("report-" + stamp.replace(":", "-") + ".html")).write_text(document)
