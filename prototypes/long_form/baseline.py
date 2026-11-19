"""Current Atlas on one request (public API, model "atlas"): python baseline.py <case> <request-file>."""

import json
import sys
import time
from pathlib import Path

import httpx

from longform.llm import API, _key, record

ROOT = Path(__file__).resolve().parent

if __name__ == "__main__":
    case, request_file = sys.argv[1], sys.argv[2]
    text = Path(request_file).read_text().strip()
    out = ROOT / "baselines" / case
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    response = httpx.post(
        API + "/chat/completions",
        headers={"Authorization": "Bearer " + _key()},
        json={"model": "atlas", "stream": False, "messages": [{"role": "user", "content": text}]},
        timeout=1800,
    )
    seconds = time.monotonic() - started
    data = response.json()
    atlas = data.get("atlas", {})
    cost = float(atlas.get("cost_eur") or 0)
    record({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "case": case, "step": "baseline:atlas", "model": "atlas", "seconds": round(seconds, 1), "eur": cost, "cost_kind": atlas.get("cost_kind")})
    (out / "atlas_baseline.json").write_text(json.dumps({"seconds": round(seconds, 1), "status": response.status_code, "response": data}, ensure_ascii=False, indent=1))
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    (out / "atlas_baseline.md").write_text(content)
    print(f"{case}: status {response.status_code}, {seconds:.0f} s, {cost:.4f} EUR, {len(content.split())} words, sources {len(atlas.get('sources') or [])}")
