"""Measure candidate writing models on real section prompts of v5 traces: python measure_writers.py > out/v6/writers.json."""
import json, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from longform import checks, write
from longform.llm import Client

CASES = [("h100", "S5"), ("etude_rd", "S7"), ("battery_en", "S6")]
MODELS = sys.argv[1].split(",") if len(sys.argv) > 1 else ["mistral/mistral-large-4", "mistral/zai-glm-5-3-flash", "scaleway/deepseek-v4-flash-0731", "mistral/zai-glm-5-3"]


def one(case, sid, model):
    t = json.loads(Path(f"out/v5/{case}/trace.json").read_text())
    step0 = t["steps"][0]
    decision, expected = step0["decision"], step0["expectations"]
    language = step0["signals"]["language"]
    outline = t["outline"]
    sections = {s["id"]: s for s in outline["sections"]}
    request = Path(f"cases/{case}.txt").read_text().strip()
    facts, derived = t["facts"], t["derived"]
    hints = {s["id"]: s.get("claim_hint", "") for s in outline["sections"] if s["id"] != sid}
    client = Client("measure:" + case, run_budget_eur=0.5)
    original = client.chat
    client.chat = lambda step, messages, **kw: original(step, messages, **{**kw, "model": model})
    started = time.monotonic()
    try:
        text = write.section(client, request, outline, sections[sid], expected, facts, derived, hints, set(), decision, language)
    except Exception as error:  # noqa: BLE001
        return {"case": case, "section": sid, "model": model, "error": str(error)[:200]}
    seconds = time.monotonic() - started
    call = client.calls[-1]
    fi = {f["id"]: f for f in facts}
    di = {d["id"]: d for d in derived}
    numbers = checks.check_numbers(sid, text, fi, di, request)
    judge = Client("measure:judge", run_budget_eur=0.5)
    support = checks.check_support(judge, sid, text, fi)
    Path(f"out/v6/writers/{case}-{sid}-{model.replace('/', '_')}.md").write_text(text)
    return {"case": case, "section": sid, "model": model, "seconds": round(seconds, 1), "eur": round(client.spent, 5),
            "completion_tokens": call.get("completion_tokens"), "reasoning_tokens": call.get("reasoning_tokens"),
            "words": len(checks.FENCE.sub("", text).split()), "tags": sum(len(checks.tags_of(u)) for u in checks.units(text)),
            "number_issues": len(numbers), "support_issues": len(support), "failed_calls": sum(1 for c in client.calls if c.get("failed"))}


if __name__ == "__main__":
    Path("out/v6/writers").mkdir(parents=True, exist_ok=True)
    jobs = [(c, s, m) for c, s in CASES for m in MODELS]
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda j: one(*j), jobs))
    print(json.dumps(rows, indent=1))
