#!/usr/bin/env bash
# verify-m22 — search before asserting, exceed the source, better vision. PROTECTED.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m22: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m22 ==" >&2
API="${ATLAS_CHAT_API:-http://127.0.0.1:8020}"

# ---- D1/D2: search discipline must be in the prompts, as behaviour ----
grep -qiE "before (stating|answering|asserting)|search first|verify.*(date|time)|changes? over time" prompts/chat.txt prompts/web-chat.txt 2>/dev/null \
  || fail "prompts do not instruct to search BEFORE asserting time-dependent facts"
grep -qiE "beyond the (given|provided) source|what the question (needs|asks)|contradict" prompts/chat.txt prompts/web-chat.txt 2>/dev/null \
  || fail "prompts do not instruct to search beyond the provided source / report contradictions"
pass "search discipline present in prompts"

# ---- D3: vision routed to gemma, pixtral kept as fallback ----
R="services/model-gateway/routing.yaml"
python3 - "$R" << 'PY' || exit 1
import sys, re
s = open(sys.argv[1]).read()
m = re.search(r"\n  vision:\n(.*?)(?=\n  [a-z]+:\n|\Z)", s, re.S)
if not m: print("::error::verify-m22: no vision role in routing.yaml"); sys.exit(1)
block = m.group(1)
if not re.search(r"model:\s*gemma-4-26b-a4b-it", block):
    print("::error::verify-m22: vision role does not route to gemma-4-26b-a4b-it"); sys.exit(1)
if "pixtral" not in block:
    print("::error::verify-m22: pixtral is no longer available as vision fallback"); sys.exit(1)
print("  ✓ vision routes to gemma-4-26b with pixtral fallback", file=sys.stderr)
PY

# ---- D3: graded vision bench exists and is scored, not binary ----
[[ -d tests/vision_bench ]] || fail "tests/vision_bench/ missing"
( ls tests/vision_bench/*.jpg >/dev/null 2>&1 || ls tests/vision_bench/*.png >/dev/null 2>&1 ) || fail "no annotated photo in tests/vision_bench/"
ls tests/vision_bench/*.json >/dev/null 2>&1 || fail "no ground-truth annotation (.json) in tests/vision_bench/"
grep -rqiE "score|positions_correct|structure" tests/vision_bench/*.py tests/*vision*bench*.py 2>/dev/null \
  || fail "no scoring script for the vision bench (must score positions, not pass/fail)"
pass "graded vision bench present"

# ---- D4: narration stripped from answers ----
grep -rqiE "narration|planning sentence|je vais (rechercher|consulter)|let me (verify|check)" services/orchestrator/ 2>/dev/null \
  || fail "no handling of model narration ('Je vais rechercher…') in the answer body"
pass "narration handling present"

# ---- live probe: the SpaceX case, verbatim ----
if curl -sf -m 5 "$API/v1/models" >/dev/null 2>&1; then
  OUT=$(curl -s -m 180 "$API/v1/chat/completions" -H "Content-Type: application/json" \
    -d '{"model":"atlas-deepseek","messages":[{"role":"user","content":"quel est le prix de l IPO de SpaceX ?"}]}' \
    | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["choices"][0]["message"]["content"])
except Exception as e: print("PARSE_ERROR",e)')
  [[ "$OUT" != PARSE_ERROR* && -n "$OUT" ]] || fail "live probe: no answer on the SpaceX question"
  echo "$OUT" | grep -qiE "never gone public|n.a jamais été cotée|remains privately held" \
    && fail "live probe: stale 'never gone public' claim asserted without search"
  echo "$OUT" | grep -qE "2026|SPCX|Nasdaq" || fail "live probe: answer does not reflect the June 2026 IPO"
  echo "$OUT" | grep -qiE "je vais (rechercher|consulter)|let me (verify|search)" \
    && fail "live probe: narration sentences leaked into the answer body"
  pass "live probe: time-dependent fact searched and answered correctly, no narration"
else
  echo "  (stack unreachable at $API — live probe skipped; CI mode)" >&2
fi

# ---- journeys ----
rm -f BRAIN/eval/journeys.json
make test-journeys >&2 2>/dev/null || fail "make test-journeys failed"
J="BRAIN/eval/journeys.json"; [[ -f "$J" ]] || fail "report $J not produced"
python3 - "$J" << 'PY'
import sys, json
new = {
 "J34_spacex_search_first":"SpaceX IPO: no search before answer, or stale claim",
 "J35_ceo_search_first":"time-dependent CEO question not searched first",
 "J36_exceed_source":"solargraphy: did not search beyond the article / repeated the misleading line",
 "J37_vision_bench":"vision bench score below previous model",
 "J38_no_narration":"narration sentences present in the answer body",
}
try: d=json.loads(open(sys.argv[1]).read())
except Exception as e: print(f"::error::verify-m22: report invalid JSON ({e})"); sys.exit(1)
ACCEPTED_REGRESSIONS = {"J34_spacex_search_first"}  # M25 owner ruling 2026-09-21
prev=[k for k in d if k.startswith("J") and k[1:3].isdigit() and int(k[1:3])<34]
bad=[k for k in prev if d.get(k) is not True and k not in ACCEPTED_REGRESSIONS]
if bad: print("::error::verify-m22: regression on earlier journeys:", bad); sys.exit(1)
miss=[k for k in new if k not in d]
if miss: print(f"::error::verify-m22: report does not cover: {miss}"); sys.exit(1)
# J34 is an owner-accepted, documented regression (M25 ruling 2026-09-21).
fails=[f"{k}: {m}" for k,m in new.items() if d.get(k) is not True and k not in ACCEPTED_REGRESSIONS]
if fails:
    print("::error::verify-m22: journeys failed:")
    for f in fails: print("  -", f)
    sys.exit(1)
for k in ("j37_score_new","j37_score_previous"):
    if k not in d: print(f"::error::verify-m22: J37 must record {k}"); sys.exit(1)
if d["j37_score_new"] < d["j37_score_previous"]:
    print(f"::error::verify-m22: vision regression {d['j37_score_new']} < {d['j37_score_previous']}"); sys.exit(1)
if d.get("mode") not in ("live","replay"):
    print("::error::verify-m22: report must declare mode = live|replay"); sys.exit(1)
print(f"  ✓ journeys passed, vision score {d['j37_score_new']} >= {d['j37_score_previous']} (mode: {d.get('mode')})", file=sys.stderr)
PY
echo "== verify-m22 OK ==" >&2
