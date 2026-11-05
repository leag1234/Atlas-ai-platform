"""Render a Markdown summary from one or more run.py result files.

Usage: python3 evals/code/report.py RESULT.json [RESULT.json ...] > summary.md
"""

import json
import sys


def cell(record):
    if record is None:
        return "not run"
    mark = "PASS" if record["passed"] else ("FAIL, false claim" if record["false_claim"] else "FAIL")
    cost = record.get("cost_eur")
    cost = f"{cost:.3f} EUR" if isinstance(cost, (int, float)) else "n/a"
    return f"{mark} ({record['wall_s']:.0f} s, {cost}, {record.get('steps', 'n/a')} steps)"


def reason(record):
    lines = [l for l in (record.get("check_tail") or "").strip().splitlines() if l.strip()]
    useful = [l for l in lines if not l.startswith(("Ran ", "---", "===", "  File", "Traceback"))]
    return (useful[-1] if useful else (lines[-1] if lines else "")).strip()[:140]


def main(paths):
    runs = [json.load(open(p)) for p in paths]
    out = ["| Configuration | Tasks | Passed | Pass rate | Median time | Cost | False claims | Timeouts | Median steps |",
           "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for run in runs:
        s = run["summary"]
        cost = s.get("cost_eur_total")
        out.append(f"| {run['config']} | {s['tasks']} | {s['passed']} | {s['pass_rate']:.0%} | {s['median_wall_s']} s | "
                   f"{'n/a' if cost is None else f'{cost:.3f} EUR'} | {s['false_claims']} | {s['timeouts']} | {s.get('median_steps')} |")
    out += ["", "| Task | Category | " + " | ".join(r["config"] for r in runs) + " |",
            "|---|---|" + "---|" * len(runs)]
    ids = sorted({rec["id"] for run in runs for rec in run["records"]})
    by = [{rec["id"]: rec for rec in run["records"]} for run in runs]
    for task in ids:
        category = next(b[task]["category"] for b in by if task in b)
        out.append(f"| {task} | {category} | " + " | ".join(cell(b.get(task)) for b in by) + " |")
    out += ["", "Failures (last line of the hidden check):", ""]
    for run, b in zip(runs, by):
        for task in ids:
            record = b.get(task)
            if record and not record["passed"]:
                out.append(f"- {run['config']} / {task}: claim `{record['claim']}`; check: `{reason(record)}`")
    print("\n".join(out))


if __name__ == "__main__":
    main(sys.argv[1:])
