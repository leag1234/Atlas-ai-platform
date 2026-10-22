#!/usr/bin/env bash
# verify-m18 — Conversation integrity + image fidelity. PROTECTED (CODEOWNERS).
#
# Same principle as M17: user journeys through the public chat API, plus a live probe
# performed by the gate itself. Adds static checks on the image settings, because the
# root cause of D6 was a configuration that disabled prompt adherence.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m18: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m18 ==" >&2
API="${ATLAS_CHAT_API:-http://127.0.0.1:8020}"

# ---- 1. Image settings: the exact values that caused D6 must be gone ----
W="services/model-gateway/image_worker.py"
M="services/model-gateway/image-model.json"
[[ -f "$W" && -f "$M" ]] || fail "image worker or model config missing"
# guidance_scale=0.0 is schnell's documented setting: NOT checked here.
# schnell is a distilled few-step model: step count is measured, not gated here.
grep -qE "max_sequence_length\s*=\s*256\b" "$W" && fail "max_sequence_length still 256: long prompts are truncated"
grep -qE "manual_seed\(\s*42\s*\)" "$W" && fail "seed is still hardcoded to 42: no variation between generations"
grep -qE "height\s*=\s*512|width\s*=\s*512" "$W" && fail "resolution still 512: raise it"
grep -qE "Timer\(\s*8[0-9]\s*," "$W" && fail "worker watchdog still ~85 s: too short for the slower model"
grep -q "FLUX.1-schnell" "$M" || fail "image model changed: this PoC stays on FLUX.1-schnell (Apache-2.0). FLUX.1-dev is gated and non-commercial."
pass "image settings fixed (no truncation, random seed, 1024px, longer watchdog) on FLUX.1-schnell"

# licence note must be documented somewhere visible
grep -rqiE "non-commercial|noncommercial|apache" runbooks/ reports/ 2>/dev/null \
  || fail "the schnell/dev licence trade-off is not documented in runbooks/ or reports/"
pass "image model licence trade-off documented"

# ---- 2. Live probe by the gate itself: follow-up must not re-route ----
if curl -sf -m 5 "$API/v1/models" >/dev/null 2>&1; then
  BODY='{"model":"atlas","messages":[
    {"role":"user","content":"Donne-moi en une phrase la définition de la photosynthèse."},
    {"role":"assistant","content":"La photosynthèse est le processus par lequel les plantes convertissent la lumière en énergie chimique."},
    {"role":"user","content":"Traduis ta réponse précédente en allemand."}]}'
  OUT=$(curl -s -m 120 "$API/v1/chat/completions" -H "Content-Type: application/json" -d "$BODY" \
        | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); print(d["choices"][0]["message"]["content"])
except Exception as e: print("PARSE_ERROR",e)')
  [[ "$OUT" != PARSE_ERROR* && -n "$OUT" ]] || fail "live probe: follow-up produced no answer ($OUT)"
  python3 - "$OUT" << 'PY' || fail "live probe: follow-up was not translated to German (likely re-routed as a new request)"
import sys,re
t=sys.argv[1].lower()
de=len(re.findall(r"\b(der|die|das|und|ist|von|durch|pflanzen|licht|in|zu)\b",t))
fr=len(re.findall(r"\b(le|la|les|est|par|des|dans|lumière|plantes)\b",t))
sys.exit(0 if de>fr else 1)
PY
  pass "live probe: follow-up operates on the previous answer (German translation)"
else
  echo "  (stack unreachable at $API — live probe skipped; CI mode)" >&2
fi

# ---- 3. Journeys ----
rm -f BRAIN/eval/journeys.json
make test-journeys >&2 2>/dev/null || fail "make test-journeys failed"
R="BRAIN/eval/journeys.json"; [[ -f "$R" ]] || fail "report $R not produced"
python3 - "$R" << 'PY'
import sys, json
req = {
 # non-regression (M17)
 "J1_plain_answer":"plain answer lost","J1_language_match":"answer language lost",
 "J2_citation_resolvable":"citations lost","J3_image_described":"vision lost",
 "J3_language_match":"vision language lost","J4_image_returned":"image generation lost",
 "J5_memory_shared":"project memory lost","J5_projects_isolated":"project isolation lost",
 "J6_tools_used":"web tools lost","J6_final_answer":"web final answer lost",
 "J7_auxiliary_ok":"auxiliary calls lost",
 # M18
 "J9_followup_after_rag":"follow-up after RAG re-routed instead of using the previous answer",
 "J10_iterate_on_image":"iterating on a generated image fails (context_exceeded?)",
 "J11_edit_intent_honest":"image edit request not answered honestly (unsolicited description)",
 "J12_web_retry":"no automatic retry after a failed web search",
 "J13_labels_fixed":"status labels are not fixed strings in the UI locale",
 "J14_settings_applied":"image settings (no truncation, random seed, 1024px, rewritten prompt) not applied",
}
try: d=json.loads(open(sys.argv[1]).read())
except Exception as e: print(f"::error::verify-m18: report invalid JSON ({e})"); sys.exit(1)
miss=[k for k in req if k not in d]
if miss: print(f"::error::verify-m18: report does not cover: {miss}"); sys.exit(1)
bad=[f"{k}: {m}" for k,m in req.items() if d.get(k) is not True]
if bad:
    print("::error::verify-m18: journeys failed:")
    for b in bad: print("  -", b)
    sys.exit(1)
if d.get("mode") not in ("live","replay"):
    print("::error::verify-m18: report must declare mode = live|replay"); sys.exit(1)
# J14 must record what was actually asked and produced
for k in ("j14_prompt","j14_rewritten_prompt","j14_seed","j14_constraints_detail"):
    if not d.get(k): print(f"::error::verify-m18: J14 must record {k}"); sys.exit(1)
score = d.get("j14_constraints_met")
if not isinstance(score, int):
    print("::error::verify-m18: J14 must report j14_constraints_met as an integer"); sys.exit(1)
if score < 3:
    print(f"  ! J14: only {score}/3 explicit constraints satisfied — reported as evidence "
          f"for the model-choice decision (not a gate failure)", file=sys.stderr)
print(f"  ✓ all journeys passed (mode: {d.get('mode')})", file=sys.stderr)
PY

echo "== verify-m18 OK ==" >&2
