"""Blind grading packs: python grading_pack.py <outA> <outB> <case> <dest>. Writes doc.md and facts.json for each label."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
src_a, src_b, case, dest = sys.argv[1:5]
for label, src in (("A", src_a), ("B", src_b)):
    base = ROOT / "out" / src / case
    target = Path(dest) / case / label
    target.mkdir(parents=True, exist_ok=True)
    doc = (base / "doc.md").read_text()
    (target / "doc.md").write_text(doc)
    trace = json.loads((base / "trace.json").read_text())
    keep = ("id", "statement", "value", "unit", "subject", "conditions", "quote", "url", "title")
    facts = [{k: f.get(k) for k in keep if k in f} for f in trace.get("facts", [])]
    cited = {f["url"] for f in facts if f["url"] in doc}
    (target / "facts.json").write_text(json.dumps([f for f in facts if f["url"] in cited], ensure_ascii=False, indent=1))
print("ok")
