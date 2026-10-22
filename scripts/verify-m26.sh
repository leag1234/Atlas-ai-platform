#!/usr/bin/env bash
# verify-m26 — live acceptance proved, not declared. PROTECTED (CODEOWNERS).
# Contract: contracts/m26-owner.md.
#
# A report's "mode" field is written by the code under test and proves nothing: the M25
# gate wrote mode="replay" itself. This gate proves live execution from evidence the code
# does not control: its own start time, the capture files' write times, and provider
# completion ids that cannot already exist in the repository.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m26: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m26 ==" >&2

IN_CI=false
[[ "${CI:-}" == "true" || "${GITHUB_ACTIONS:-}" == "true" ]] && IN_CI=true

# ---- D1: a gate that cannot run says why ----------------------------------
if python3 - <<'PY'
import socket, sys
s = socket.socket(); s.settimeout(0.5)
sys.exit(0 if s.connect_ex(("127.0.0.1", 8020)) == 0 else 1)
PY
then
  fail "port 8020 is already in use (a running stack?). The journeys start their own server. Stop it first, e.g. tmux kill-session -t stack"
fi
pass "port 8020 free"

# ---- D2: the owner's PDF never enters the repository -----------------------
# Not "git ls-files | grep -q": under pipefail grep -q exits early, git ls-files gets
# SIGPIPE on a large repository, the pipeline returns 141 and the check silently passes.
TRACKED="$(git ls-files)"
if grep -qiE 'owner-174p|174p.*\.pdf' <<<"$TRACKED"; then
  fail "the owner's PDF is tracked by git; it must stay at /opt/atlas-src/private/fixtures/owner-174p.pdf"
fi
pass "owner fixture not in the repository"

# ---- D3: the known defect, checked exactly ---------------------------------
if grep -nE 'has_attachments=bool\(request\.documents\)' services/orchestrator/chat_pipeline.py >&2; then
  fail "has_attachments still derives from request.documents alone: a terminal-routed file leaves it false and the 0.30 EUR ceiling never applies"
fi
pass "attachment flag no longer derived from request.documents alone"

# ---- regressions on earlier journeys (replay is fine for this) -------------
make test-journeys >&2 || fail "make test-journeys failed (regression on earlier journeys)"
pass "earlier journeys, no regression"

if [[ "$IN_CI" == "true" ]]; then
  echo "  (CI: live acceptance is not run here; it is required locally before merge)" >&2
  echo "== verify-m26 OK (CI) ==" >&2
  exit 0
fi

# ---- D1: live acceptance, proved -------------------------------------------
T0="$(date +%s)"
rm -rf BRAIN/m26
env -u M25_REPLAY -u M24_REPLAY -u M23_REPLAY -u ATLAS_M3_EVAL_MODE \
    JOURNEYS_LIVE=1 M26_LIVE=1 make m26-live >&2 \
  || fail "make m26-live failed"

python3 - "$T0" <<'PY'
import gzip, json, os, re, subprocess, sys
from pathlib import Path

t0 = int(sys.argv[1])
def fail(msg):
    print(f"::error::verify-m26: {msg}", file=sys.stderr); sys.exit(1)

rp = Path("BRAIN/m26/report.json")
if not rp.is_file(): fail(f"{rp} not produced by make m26-live")
if rp.stat().st_mtime < t0: fail(f"{rp} predates this gate run: not a fresh execution")
try: report = json.loads(rp.read_text())
except Exception as e: fail(f"{rp} is not valid JSON ({e})")

journeys = ["J54", "J55", "J56", "J57", "J58", "J59", "J60"]
missing = [j for j in journeys if j not in report]
if missing: fail(f"report does not cover {missing}")

notrun = [j for j in journeys if str(report[j].get("status")).upper() == "NOT_RUN"]
if notrun: fail(f"journeys NOT RUN (reported, never passed): {notrun}")
failed = [j for j in journeys if report[j].get("status") is not True]
if failed: fail(f"journeys failed: {failed}")

def tracked_contains(needle):
    r = subprocess.run(["git", "grep", "-q", "-F", needle], capture_output=True)
    return r.returncode == 0

captures = {}
for j in journeys:
    e = report[j]
    if e.get("mode") != "live": fail(f"{j} mode is {e.get('mode')!r}, not live")
    if j == "J60":
        continue  # refused before any provider call: no capture, no cost by design
    if not isinstance(e.get("seconds"), (int, float)): fail(f"{j} has no measured seconds")
    if not (isinstance(e.get("cost_eur"), (int, float)) and e["cost_eur"] > 0):
        fail(f"{j} cost_eur must be > 0 for a live provider call, got {e.get('cost_eur')!r}")
    cp = Path(e.get("capture") or "")
    if not cp.is_file(): fail(f"{j} capture {cp} missing")
    if cp.stat().st_mtime < t0: fail(f"{j} capture {cp} predates this gate run: replayed, not live")
    try: cap = json.loads(gzip.decompress(cp.read_bytes()))
    except Exception as ex: fail(f"{j} capture {cp} unreadable ({ex})")
    rows = cap.get("rows") or []
    streams = [r for r in rows if r.get("kind") == "stream"]
    if not streams: fail(f"{j} capture records no provider stream")
    ids = [r.get("provider_id") for r in streams]
    if not all(isinstance(i, str) and i for i in ids):
        fail(f"{j} a stream row has no provider_id (the provider's own completion id)")
    for i in ids:
        if tracked_contains(i):
            fail(f"{j} provider id {i} already exists in the repository: this is a replayed recording, not a live call")
    captures[j] = cap

# J54: both files delivered
names = [str(n).lower() for n in report["J54"].get("files", [])]
if not any(n.endswith(".pptx") for n in names): fail("J54 no .pptx delivered")
if not any(n.endswith(".pdf") for n in names): fail("J54 no .pdf delivered")

# J55: the measured deadline and ceiling, and the strategy stated
e = report["J55"]
if e.get("deadline_seconds") != 300: fail(f"J55 deadline_seconds is {e.get('deadline_seconds')!r}, expected 300")
if e.get("attachment_ceiling_eur") not in (0.3, 0.30): fail(f"J55 attachment_ceiling_eur is {e.get('attachment_ceiling_eur')!r}, expected 0.30")
if not isinstance(e.get("hierarchical"), bool): fail("J55 must state whether hierarchical synthesis was used")

# J56: the code is in the answer
if "```" not in (captures["J56"].get("answer") or ""): fail("J56 the answer contains no code block")

# J59: searched, and every URL was retrieved
cap = captures["J59"]
rows = cap.get("rows") or []
retrieved = [r for r in rows if r.get("kind") in ("search", "fetch")]
if not retrieved: fail("J59 zero search/fetch calls: sources cannot have been retrieved")
blob = json.dumps(retrieved, ensure_ascii=False)
urls = sorted(set(u.rstrip(".,;:)]}>'\"") for u in re.findall(r"https?://[^\s<>\"'()\]]+", cap.get("answer") or "")))
invented = [u for u in urls if u not in blob]
if invented: fail(f"J59 URLs cited but never retrieved: {invented}")

# J60: a budget refusal is not a 504, and it says what was measured (R6)
e = report["J60"]
status = e.get("http_status")
if not isinstance(status, int): fail("J60 must record http_status")
if status in (502, 503, 504): fail(f"J60 a local budget refusal reached the caller as HTTP {status}")
msg = str(e.get("message") or "")
for k in ("measured_microeur", "limit_microeur"):
    if not isinstance(e.get(k), int): fail(f"J60 must record {k}")
    if str(e[k]) not in msg: fail(f"J60 the message does not contain the {k.split('_')[0]} value {e[k]}")

print(f"  ✓ J54–J60 passed LIVE, proved by fresh captures and unseen provider ids; "
      f"J59 cited {len(urls)} URL(s), all retrieved", file=sys.stderr)
PY

echo "== verify-m26 OK ==" >&2
