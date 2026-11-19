"""Side-by-side metrics, current Atlas vs prototype: python metrics.py > out/metrics.json."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
import sys
OUT = sys.argv[1] if len(sys.argv) > 1 else "."
CASES = ["h100", "etude_rd", "mrna_fr", "battery_en", "heating_en", "linux_pme_fr", "quick_fr", "quick_en"]


def text_metrics(text: str) -> dict:
    prose = re.sub(r"```[\s\S]*?```", "", text)
    return {
        "words": len(prose.split()),
        "links": len(set(re.findall(r"https?://[^\s)>\]]+", text))),
        "mermaid": text.count("```mermaid"),
        "other_script": bool(re.search(r"[Ѐ-ӿ぀-ヿ一-鿿]", prose)),
    }


rows = {}
for case in CASES:
    row = {}
    base = ROOT / "baselines" / case / "atlas_baseline.json"
    if base.exists():
        data = json.loads(base.read_text())
        response = data["response"]
        content = (response.get("choices") or [{}])[0].get("message", {}).get("content", "") if response.get("choices") else ""
        atlas = response.get("atlas", {})
        row["atlas"] = {"seconds": data["seconds"], "eur": atlas.get("cost_eur"), "cost_kind": atlas.get("cost_kind"), "status": data["status"], **text_metrics(content)}
    trace = ROOT / "out" / OUT / case / "trace.json"
    if trace.exists():
        t = json.loads(trace.read_text())
        doc = (ROOT / "out" / OUT / case / "doc.md").read_text()
        final = t.get("checks_final") or {}
        stats = final.get("number_stats", {})
        row["prototype"] = {
            "seconds": t["seconds"],
            "eur": t["cost_eur"],
            "path": t.get("path", "long"),
            "sources_cited": t.get("sources_cited"),
            "facts": len(t.get("facts", [])),
            "derived": len(t.get("derived", [])),
            "issues_first": len((t.get("checks_first") or {}).get("issues", [])),
            "issues_final": len(final.get("issues", [])),
            "numbers_sourced": sum(s["sourced"] for s in stats.values()),
            "numbers_computed": sum(s["computed"] for s in stats.values()),
            "numbers_hypothesis_or_unverified": sum(s["hypothesis"] for s in stats.values()),
            "failed_calls": sum(1 for c in t.get("calls", []) if c.get("failed")),
            "web_searches": (t.get("search_state") or {}).get("count", 0),
            **text_metrics(doc),
        }
    rows[case] = row
print(json.dumps(rows, ensure_ascii=False, indent=1))
