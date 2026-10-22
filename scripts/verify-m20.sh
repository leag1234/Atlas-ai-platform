#!/usr/bin/env bash
# verify-m20 — real-usage journeys, honest failures, unified startup. PROTECTED.
#
# Guards the failure mode of M17/M18/M19: green gates while the most ordinary usage is
# broken. Checks (a) journey quality, (b) delivery reality, (c) the session's defects,
# (d) session efficiency.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m20: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m20 ==" >&2
J="tests/journeys"; [[ -d "$J" ]] || fail "$J missing"
BODY=$(cat "$J"/*.py 2>/dev/null || true); [[ -n "$BODY" ]] || fail "no journey files"

# ---------- (a) journey quality (R1..R4) ----------
# R4: verbatim phrasings from real sessions
for s in "dessine-moi un mouton" "décris cette image" "intègre ces deux images" "modifier cette image"; do
  grep -qiF "$s" <<< "$BODY" || fail "verbatim case missing: \"$s\" (a real session failed on it)"
done
pass "verbatim regressions from real sessions are covered"

# R2: at least 3 image phrasings without the word "image"
N=0
for s in "dessine-moi un mouton" "fais-moi un portrait" "je voudrais voir" "draw me a" "DESSINE UN"; do
  grep -qiF "$s" <<< "$BODY" && N=$((N+1))
done
[[ "$N" -ge 3 ]] || fail "only $N image phrasings without the word 'image' (need >= 3)"
grep -qiF "decris cette image" <<< "$BODY" || fail "missing unaccented French case"
grep -qE "[A-ZÉÈ]{6,}" <<< "$BODY" || fail "missing uppercase case"
grep -qiE "was ist|draw me|describe this" <<< "$BODY" || fail "missing non-French phrasing"
pass "$N keyword-free phrasings; short/unaccented/uppercase/non-French cases present"

# R2: multiple attachments (the 2026-09-15 failure)
grep -qiE "two images|deux images|images=\[.*,.*\]|attachments.*2" <<< "$BODY" \
  || fail "no journey sends TWO attachments: that exact case failed in production"
pass "multi-attachment journey present"

# negative cases
NEG=0
for s in "analyse cette image" "décris l'image ci-jointe" "modifier cette image"; do
  grep -qiF "$s" <<< "$BODY" && NEG=$((NEG+1))
done
[[ "$NEG" -ge 2 ]] || fail "only $NEG negative cases (need >= 2)"
pass "$NEG negative cases present"

# R3: no assertions on internals
grep -qE "assert .*(image_request|chat_pipeline|process_vision|process_image)\(" <<< "$BODY" \
  && fail "journeys assert on internal functions; they must assert on what the user receives"
pass "assertions are user-visible only"

# ---------- (b) delivery reality (R5) ----------
# every service module must have a caller outside tests/
ORPHANS=""
while IFS= read -r f; do
  mod=$(basename "$f" .py)
  case "$mod" in __init__|conftest) continue;; esac
  if ! grep -rqE "(import|from)[^#]*\b${mod}\b" --include="*.py" services/ packages/ 2>/dev/null \
       --exclude="$(basename "$f")"; then
    ORPHANS="$ORPHANS $mod"
  fi
done < <(find services -name "*.py" -not -path "*/tests/*" 2>/dev/null)
[[ -z "$ORPHANS" ]] || echo "  ! modules with no caller outside tests:$ORPHANS" >&2
pass "delivery-reality check performed"

# the image rewrite must be reachable from the real generation path
grep -rq "image_prompt\|rewrite" services/model-gateway/imagegen.py 2>/dev/null \
  || fail "image rewriting is not wired into the real generation path"
pass "image rewriting wired into the generation path"

# ---------- (c) this session's defects ----------
# D3: generation must log rewritten prompt and seed
grep -rqE "rewritten_prompt|prompt_reecrit" services/ 2>/dev/null \
  || fail "the rewritten prompt is not logged (a bad image cannot be diagnosed)"
grep -rqE "\"seed\"|'seed'" services/orchestrator/ services/model-gateway/ 2>/dev/null \
  || fail "the seed is not logged (an image cannot be reproduced)"
pass "rewritten prompt and seed are logged"

# D6: required variables validated at startup
grep -rqiE "missing (required )?(environment|variable)|required_env|REQUIRED_VARS" services/ 2>/dev/null \
  || fail "no startup validation of required variables (GPU_CLIENT_IP hung the service)"
pass "required variables validated at startup"

# D7: gpu-up must not announce success before creating
grep -qE "submitted; readiness checked" infra/gpu.py 2>/dev/null \
  && fail "gpu-up still announces success before the instance exists"
pass "gpu-up no longer announces a false success"

# D5: on-demand provisioning of the image worker
grep -rqiE "provision|start.*worker|ensure_worker" services/orchestrator/imagegen.py 2>/dev/null \
  || fail "the image worker is not started on demand (user must run make serve-imagegen)"
pass "image worker starts on demand"

# R6: rejections report measured values
grep -rqE "context_exceeded" services/ 2>/dev/null && {
  grep -rqE "context_exceeded.*(tokens|limit)|f\"context_exceeded" services/ 2>/dev/null \
    || fail "context_exceeded is raised without reporting measured values against the limit"
}
pass "rejections report what was measured"

# ---------- (d) session efficiency ----------
grep -qiE "do NOT re-verify|not re-verify|skip.*re-?verif" MISSION.md AGENTS.md 2>/dev/null \
  || fail "no rule preventing re-verification of completed milestones"
SZ=$(wc -c < MISSION.md)
[[ "$SZ" -le 8000 ]] || fail "MISSION.md is $SZ bytes (> 8000): move settled decisions to docs/decisions-log.md"
[[ -f docs/decisions-log.md ]] || fail "docs/decisions-log.md missing"
pass "re-verification rule in place; MISSION.md lean ($SZ bytes)"

# ---------- journeys report ----------
rm -f BRAIN/eval/journeys.json
make test-journeys >&2 2>/dev/null || fail "make test-journeys failed"
R="BRAIN/eval/journeys.json"; [[ -f "$R" ]] || fail "report $R not produced"
python3 - "$R" << 'PY'
import sys, json
new = {
 "J21_two_images_honest":"two attached images + 'intègre ces deux images' still fails",
 "J22_two_images_compare":"comparing two attached images fails",
 "J23_edit_intent_honest":"'tu saurais modifier cette image ?' not answered honestly",
 "J24_constraints_reported":"multi-subject constraints not reported per constraint",
 "J25_worker_on_demand":"image worker not provisioned on demand",
 "J26_missing_var_refused":"missing required variable does not produce an explicit refusal",
}
try: d=json.loads(open(sys.argv[1]).read())
except Exception as e: print(f"::error::verify-m20: report invalid JSON ({e})"); sys.exit(1)
prev=[k for k in d if k.startswith("J") and k[1:3].isdigit() and int(k[1:3])<21]
bad=[k for k in prev if d.get(k) is not True]
if bad:
    print("::error::verify-m20: regression on earlier journeys:", bad); sys.exit(1)
miss=[k for k in new if k not in d]
if miss: print(f"::error::verify-m20: report does not cover: {miss}"); sys.exit(1)
fails=[f"{k}: {m}" for k,m in new.items() if d.get(k) is not True]
if fails:
    print("::error::verify-m20: journeys failed:")
    for f in fails: print("  -", f)
    sys.exit(1)
for k in ("j24_prompt","j24_rewritten_prompt","j24_seed","j24_constraints_detail"):
    if not d.get(k): print(f"::error::verify-m20: J24 must record {k}"); sys.exit(1)
if d.get("mode") not in ("live","replay"):
    print("::error::verify-m20: report must declare mode = live|replay"); sys.exit(1)
print(f"  ✓ journeys passed, no regression (mode: {d.get('mode')})", file=sys.stderr)
PY

echo "== verify-m20 OK ==" >&2
