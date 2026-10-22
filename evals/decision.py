"""POC M6: a missing measurement cannot produce a GO decision."""

import gzip
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any


def percentile(values: list[float]) -> float | None:
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("invalid_measurement")
    return sorted(values)[math.ceil(0.95 * len(values)) - 1] if values else None


def render(
    quality: dict[str, Any],
    bench: dict[str, Any],
    demo: list[dict[str, Any]],
    calibration: dict[str, Any],
) -> str:
    if set(quality.get("suites", {})) != {f"e{i}" for i in range(1, 10)}:
        raise ValueError("missing_quality_suites")
    if {r["id"] for r in demo} != {"DEMO-RAG", "DEMO-WEB", "DEMO-FALLBACK"}:
        raise ValueError("incomplete_demo")
    requests = [bench["baseline"]] + [r for wave in bench["waves"] for r in wave]

    def p95(key: str) -> float | None:
        return percentile(
            [
                r["telemetry"][key]
                for r in requests
                if r["telemetry"].get(key) is not None
            ]
        )

    baseline = bench["baseline"]["telemetry"]["latency"]
    if baseline <= 0 or any(len(wave) != 8 for wave in bench["waves"]):
        raise ValueError("invalid_load_bench")
    degradation = max(
        mean(r["telemetry"]["latency"] for r in wave) / baseline - 1
        for wave in bench["waves"]
    )
    rows: list[tuple[str, Any, str, bool, str]] = []

    def metric(
        ident: str,
        value: float | None,
        threshold: float,
        greater: bool,
        source: str,
        sufficient: bool = True,
    ) -> None:
        if value is not None and not math.isfinite(value):
            raise ValueError("invalid_measurement")
        passed = (
            sufficient
            and value is not None
            and (value > threshold if greater else value < threshold)
        )
        rows.append(
            (ident, value, ("> " if greater else "< ") + str(threshold), passed, source)
        )

    metric("P1", p95("ttft"), 2.0, False, "TTFT p95, serverless, secondes")
    metric("P2", p95("tok_s"), 30.0, True, "décodage p95, serverless, tok/s")
    for ident, case, limit in (("P3", "DEMO-RAG", 12.0), ("P4", "DEMO-WEB", 30.0)):
        metric(
            ident,
            next(r["latency"] for r in demo if r["id"] == case),
            limit,
            False,
            "une requête live, secondes ; pas un p95 de charge",
            sufficient=False,
        )
    rows.append(
        (
            "P5",
            degradation,
            "≤ 0.2",
            degradation <= 0.2,
            "dégradation maximale moyenne, deux vagues de huit",
        )
    )
    rows.append(
        (
            "P6",
            "10 outils / 120 s / 0,05 EUR",
            "tests des budgets durs",
            bench.get("budget_tests") is True,
            "make test-budgets enregistré",
        )
    )
    metric(
        "P7",
        bench.get("local_cache_hit_ratio"),
        0.5,
        True,
        "cache vLLM local ; aucun GPU M6",
    )
    serverless_cost = mean(
        [r["telemetry"]["cost"] for r in requests] + [r["cost"] for r in demo]
    )
    metric(
        "P8",
        bench.get("amortized_request_cost"),
        0.02,
        False,
        f"GPU amorti absent ; moyenne serverless bench + démo, hors diagnostics : {serverless_cost:.8f} EUR",
    )
    uptime = bench.get("uptime_ratio") if bench.get("uptime_days", 0) >= 14 else None
    metric("P9", uptime, 0.97, True, "uptime sur deux semaines, historique absent")
    decision = "GO" if quality["quality_go"] and all(r[3] for r in rows) else "NO-GO"
    output = [
        f"# Décision : {decision}",
        "",
        "Rapport généré depuis les évals et mesures archivées, sans valeurs simulées.",
        "Sources : reports/bench.json.gz, reports/demo.json.gz ; évaluation et calibration : make eval (voir reports/M5.md).",
        "",
        "| Critère | Mesure | Cible | Critère prouvé | Provenance / limite |",
        "|---|---:|---|---|---|",
    ]
    for ident, value, target, passed, source in rows:
        output.append(
            f"| {ident} | {value if value is not None else 'indisponible'} | {target} | {'oui' if passed else 'non'} | {source} |"
        )
    output += ["", "## Qualité", "", "| Suite | Score normalisé |", "|---|---:|"]
    output += [
        f"| {name.upper()} | {value['score']:.4f} |"
        for name, value in sorted(quality["suites"].items())
    ]
    output += [
        "",
        f"Calibration croisée : κ={calibration['kappa']:.4f}. GO qualité M5 : {quality['quality_go']}.",
        "",
        "Le micro-bench mesure le serverless, pas le GPU local. Les parcours RAG/web sont des observations unitaires, pas une distribution de charge. Les durées de rejeu CI ne sont pas des temps d'inférence.",
        "",
        "Écarts conservés : citation E2-30 incorrecte, source web E6-004 périmée, terminologie E9-004 imprécise, écarts linguistiques. L'adaptateur RAG agentique tronque à 400 octets et peut masquer une réponse présente.",
        "",
        "Actions avant GO : corriger ces écarts, mesurer le cache et le coût GPU amorti, effectuer une charge RAG/web représentative et collecter quatorze jours d'uptime. Décision humaine de phase 1 requise.",
    ]
    return "\n".join(output) + "\n"


def main() -> None:
    root = Path("BRAIN/eval")
    bench = json.loads(gzip.decompress(Path("reports/bench.json.gz").read_bytes()))
    demo = json.loads(gzip.decompress(Path("reports/demo.json.gz").read_bytes()))
    output = render(
        json.loads((root / "report.json").read_text()),
        bench,
        demo,
        json.loads((root / "calibration.json").read_text()),
    )
    Path("reports/GO-NOGO.md").write_text(output)
    print("Rapport GO/NO-GO généré depuis les mesures archivées.")


if __name__ == "__main__":
    main()
