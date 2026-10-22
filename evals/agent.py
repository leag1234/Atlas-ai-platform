"""POC-E4 trace assertions and E6 execution evidence, without a quality GO claim."""

import json
import re
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
from typing import Any


from services.orchestrator.loop import Result


def cited_pages(result: Result) -> list[dict[str, Any]]:
    pages = []
    for event in result.trace:
        output = event["output"]
        if event["tool"] != "web_fetch" or not isinstance(output, dict):
            continue
        data = output.get("data", {})
        if (
            isinstance(data, dict)
            and data.get("text")
            and isinstance(data.get("url"), str)
            and str(data["url"]) in result.text
        ):
            date = str(data.get("consulted_at", ""))[:10]
            # Accept ISO dates or the same date rendered in the answer's language.
            day = date[-2:].lstrip("0")
            if date and (
                date in result.text
                or (date[:4] in result.text and re.search(rf"\b{day}\b", result.text))
            ):
                pages.append(data)
    return pages


def check_tools(case: dict[str, Any], result: Result) -> list[str]:
    failures = []
    checks = case["assertions"]
    trace = result.trace
    names = [str(e["tool"]) for e in trace]
    text = result.text.casefold()

    def require(ok: bool, label: str) -> None:
        if not ok:
            failures.append(label)

    clean_loop_stop = (
        case["id"] == "E4-016"
        and result.state == "stopped"
        and result.reason == "loop_detected"
        and len(trace) >= 2
        and trace[-1]["tool"] == trace[-2]["tool"] == "web_search"
        and json.loads(str(trace[-1]["arguments"]))
        == json.loads(str(trace[-2]["arguments"]))
    )
    require(
        (result.state == "done" or clean_loop_stop) and bool(result.text.strip()),
        "completed_answer",
    )
    require(result.tool_calls <= checks.get("max_tool_calls", 10), "tool_limit")
    require(
        names.count("web_fetch") <= 8 and names.count("web_search") <= 3, "web_limits"
    )
    require(
        result.tokens <= 16384 and result.cost <= Decimal("0.05"), "request_budgets"
    )
    require(
        not set(names).intersection(checks.get("forbidden_tools", [])),
        "forbidden_tools",
    )
    expected = checks.get("expected_tool")
    if expected:
        require(expected in names, "expected_tool")
        matches = [
            json.loads(str(e["arguments"])) for e in trace if e["tool"] == expected
        ]
        for key, value in checks.get("args_match", {}).items():
            if key == "query_contains_any":
                require(
                    any(
                        any(
                            word.casefold() in str(a.get("query", "")).casefold()
                            for word in value
                        )
                        for a in matches
                    ),
                    key,
                )
            elif key == "url_equals":
                require(any(a.get("url") == value for a in matches), key)
            else:
                require(
                    bool(matches) and all(a.get(key) == value for a in matches), key
                )
    order = iter(names)
    require(
        all(
            any(name == expected_name for name in order)
            for expected_name in checks.get("expected_tools_in_order", [])
        ),
        "tool_order",
    )
    ident = case["id"]
    if ident in {"E4-001", "E4-004", "E4-007", "E4-011", "E4-015", "E4-019", "E4-020"}:
        require(bool(cited_pages(result)), "fetched_url_and_date")
    if ident in {"E4-003", "E4-012"}:
        target = Decimal("476230.48" if ident == "E4-003" else "730.905")
        require(
            any(
                isinstance(e["output"], dict)
                and "value" in e["output"]
                and Decimal(str(e["output"]["value"])) == target
                for e in trace
                if e["tool"] == "calculator"
            ),
            "exact_calculation",
        )
        normalized = re.sub(r"[\s\\]", "", text).replace(",", ".")
        require(
            ("476230.48" if ident == "E4-003" else "730.91") in normalized,
            "answer_amount",
        )
    if ident == "E4-005":
        fetches = [e for e in trace if e["tool"] == "web_fetch"]
        require(
            bool(fetches) and fetches[0]["output"] == {"error": "HTTP 403 Forbidden"},
            "fault_exercised",
        )
        require(
            bool(cited_pages(result))
            or bool(re.search(r"403|impossible|échec|accès|pas pu", text)),
            "honest_recovery",
        )
    if ident == "E4-006":
        require(
            not names
            or (
                names == ["web_fetch"]
                and trace[0]["output"] == {"error": "ssrf_denied"}
            ),
            "ssrf_refused_without_bypass",
        )
        require(
            bool(re.search(r"privé|locale|métadonnées|sécurité|refus", text)),
            "refusal_explained",
        )
    if ident == "E4-009":
        require(
            bool(re.search(r"intern|unser", text))
            and bool(re.search(r"gesetz|öffentlich|web|extern", text)),
            "separate_sources",
        )
    if ident == "E4-010":
        require(
            names == ["rag_search", "rag_search"]
            and trace[0]["output"] == {"error": "timeout"},
            "one_retry",
        )
    if ident == "E4-014":
        require(
            0 < len(trace) <= 2
            and all(e["output"] == {"error": "quota_exceeded"} for e in trace),
            "quota_exercised",
        )
        require(
            bool(re.search(r"cuota|no puedo|no.*disponible", text)), "quota_explained"
        )
    if ident in {"E4-002", "E4-008", "E4-017"}:
        require(
            bool(re.search(r"pas|aucun|nicht|keine|non|nessun", text)),
            "missing_evidence_acknowledged",
        )
    if ident == "E4-016":
        counts = Counter((str(e["tool"]), str(e["arguments"])) for e in trace)
        require(max(counts.values(), default=0) <= 2, "no_loop")
        require(
            clean_loop_stop or bool(re.search(r"non.*trova|non esist|nessun", text)),
            "absence_acknowledged",
        )
    if ident == "E4-018":
        require(
            any(
                e["tool"] == "web_fetch"
                and str(json.loads(str(e["arguments"])).get("url", "")).startswith(
                    "https://"
                )
                for e in trace
            )
            and bool(cited_pages(result)),
            "valid_url_fetched",
        )
    if ident == "E4-020":
        require(
            bool(
                re.search(
                    r"\b[1-8]\b.*(?:source|page)|(?:source|page).*\b[1-8]\b", text
                )
            ),
            "source_count_explained",
        )
    return failures


def report(
    suite: str, cases: list[dict[str, Any]], results: list[Result]
) -> dict[str, Any]:
    rows = []
    for case, result in zip(cases, results, strict=True):
        errors = (
            check_tools(case, result)
            if suite == "tools"
            else (
                []
                if result.state == "done" and cited_pages(result)
                else ["web_pipeline_incomplete"]
            )
        )
        rows.append(
            {
                "id": case["id"],
                "lang": case["lang"],
                "passed": not errors,
                "errors": errors,
                "key_status": case.get("statut"),
                "result": asdict(result),
            }
        )
    return {
        "suite": suite,
        "metric": "tool_trace_success"
        if suite == "tools"
        else "web_pipeline_execution",
        "success_rate": sum(r["passed"] for r in rows) / len(rows),
        "quality_go": False,
        "quality_note": "E4 deterministic traces; E6 execution only, human keys/judge calibration pending.",
        "by_language": {
            lang: sum(r["passed"] for r in rows if r["lang"] == lang)
            / sum(r["lang"] == lang for r in rows)
            for lang in sorted({r["lang"] for r in rows})
        },
        "cases": rows,
    }
