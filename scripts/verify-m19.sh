#!/usr/bin/env bash
# verify-m19 — Explicit intent, explicit language, locked settings. PROTECTED.
#
# Three layers, because the same defects came back twice:
#  (a) static locks on settings that silently regressed before;
#  (b) a live probe run by the gate itself on the exact phrasings that failed;
#  (c) the journeys report.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m19: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m19 ==" >&2
API="${ATLAS_CHAT_API:-http://127.0.0.1:8020}"

# ---------- (a) static locks ----------
P="prompts/chat.txt"
[[ -f "$P" ]] || fail "$P missing"
grep -qiE "generat[a-z]* (an? )?image|image generation" "$P" || fail "$P does not declare IMAGE GENERATION"
grep -qiE "analys[a-z]* (an? )?image|image analysis|vision"  "$P" || fail "$P does not declare IMAGE ANALYSIS"
pass "system prompt declares image analysis and generation"

# every model-facing prompt must carry a language instruction
for f in prompts/chat.txt prompts/vision.txt prompts/web-chat.txt; do
  [[ -f "$f" ]] || continue
  grep -qiE "language|langue|answer in" "$f" || fail "$f carries no language instruction"
done
pass "model-facing prompts carry a language instruction"

grep -qE "max 180 attempts" scripts/run_agent_auto.sh || fail "CI wait is not 180 attempts (regressed to 60 before)"
W="services/model-gateway/image_worker.py"
grep -qE "max_sequence_length\s*=\s*512" "$W" || fail "max_sequence_length is not 512 (prompt truncation returns)"
grep -qE "height\s*=\s*1024" "$W" || fail "image height is not 1024"
grep -qE "manual_seed\(\s*42\s*\)" "$W" && fail "seed is hardcoded to 42 again"
grep -qE "Timer\(\s*(1[89][0-9]|[2-9][0-9]{2})\s*," "$W" || fail "image worker watchdog is below 180 s"
grep -q "FLUX.1-schnell" services/model-gateway/image-model.json || fail "image model is no longer FLUX.1-schnell (Apache-2.0)"
pass "settings locked (prompt length, resolution, seed, watchdog, model)"

# the settings lock must exist as a real test, run by the standard suite
[[ -f tests/test_settings_lock.py ]] || fail "tests/test_settings_lock.py missing: settings must be locked by a test, not only by this gate"
pass "settings lock test present"

# image routing must no longer depend solely on a regex
grep -rqE "generate_image" services/orchestrator/ 2>/dev/null || fail "no generate_image tool declared: the model must decide, not a regex"
pass "generate_image is declared as a tool"

# ---------- (b) live probe on the exact phrasings that failed ----------
if curl -sf -m 5 "$API/v1/models" >/dev/null 2>&1; then
  ask() {  # $1 = user message -> prints the assistant reply
    curl -s -m 180 "$API/v1/chat/completions" -H "Content-Type: application/json" \
      -d "$(python3 -c 'import json,sys; print(json.dumps({"model":"atlas","messages":[{"role":"user","content":sys.argv[1]}]}))' "$1")" \
      | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); print(d["choices"][0]["message"]["content"])
except Exception as e: print("PARSE_ERROR", e)'
  }
  # the assistant must not deny its own capabilities
  R=$(ask "Peux-tu générer des images ?")
  echo "$R" | grep -qiE "je ne peux pas|assistant textuel|dall[- ]?e|midjourney|stable diffusion" \
    && fail "the assistant denies image generation or redirects to a third-party tool: $R"
  python3 - "$R" << 'PY' || fail "capability answer is not in French"
import sys,re
t=re.sub(r"```.*?```","",sys.argv[1],flags=re.S).lower()
fr=len(re.findall(r"\b(je|peux|des|les|une|oui|vous|pour|et|analyser)\b",t))
en=len(re.findall(r"\b(the|is|can|and|to|of|images?)\b",t))
sys.exit(0 if fr>=en else 1)
PY
  pass "live probe: the assistant acknowledges image generation, in French"
else
  echo "  (stack unreachable at $API — live probe skipped; CI mode)" >&2
fi

# ---------- (c) journeys ----------
rm -f BRAIN/eval/journeys.json
make test-journeys >&2 2>/dev/null || fail "make test-journeys failed"
R="BRAIN/eval/journeys.json"; [[ -f "$R" ]] || fail "report $R not produced"
python3 - "$R" << 'PY'
import sys, json
req = {
 # M17/M18 non-regression
 "J1_plain_answer":"plain answer","J1_language_match":"answer language",
 "J2_citation_resolvable":"citations","J3_image_described":"vision",
 "J3_language_match":"vision language","J4_image_returned":"image generation",
 "J5_memory_shared":"project memory","J5_projects_isolated":"project isolation",
 "J6_tools_used":"web tools","J6_final_answer":"web final answer",
 "J7_auxiliary_ok":"auxiliary calls","J9_followup_after_rag":"follow-up after RAG",
 "J10_iterate_on_image":"iterate on generated image","J11_edit_intent_honest":"honest edit answer",
 "J12_web_retry":"web retry","J13_labels_fixed":"fixed labels",
 # M19
 "J15_draw_me_phrasing":"'dessine-moi un mouton…' did not produce an image",
 "J16_other_phrasings":"'fais-moi un portrait' / 'je voudrais voir' did not produce images",
 "J17_no_false_generation":"'analyse/décris cette image' wrongly triggered generation",
 "J18_capability_acknowledged":"the assistant denied image generation or redirected elsewhere",
 "J19_short_message_language":"3-word French message answered in another language",
 "J20_language_consistent":"language drifted during a conversation with web + image",
}
try: d=json.loads(open(sys.argv[1]).read())
except Exception as e: print(f"::error::verify-m19: report invalid JSON ({e})"); sys.exit(1)
miss=[k for k in req if k not in d]
if miss: print(f"::error::verify-m19: report does not cover: {miss}"); sys.exit(1)
bad=[f"{k}: {m}" for k,m in req.items() if d.get(k) is not True]
if bad:
    print("::error::verify-m19: journeys failed:")
    for b in bad: print("  -", b)
    sys.exit(1)
if d.get("mode") not in ("live","replay"):
    print("::error::verify-m19: report must declare mode = live|replay"); sys.exit(1)
print(f"  ✓ all journeys passed (mode: {d.get('mode')})", file=sys.stderr)
PY

echo "== verify-m19 OK ==" >&2
