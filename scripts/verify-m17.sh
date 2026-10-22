#!/usr/bin/env bash
# verify-m17 — Integration gate: USER JOURNEYS through the public chat API. PROTECTED.
#
# Exists because M8..M15 passed component checks while the product was unusable.
# Two layers of defence against a gate that could be satisfied without doing the work:
#  (a) the gate PERFORMS ITS OWN real call to /v1/chat/completions and inspects the reply
#      (a report alone can be fabricated; a live reply cannot);
#  (b) it then reads the journeys report and requires every assertion to be true.
# GPU/web journeys (J4, J6) are live on the VM and replayed from cassettes in CI.
# J8 (serve idempotence) is a separate target, never run here (it restarts the stack).
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m17: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m17 ==" >&2
API="${ATLAS_CHAT_API:-http://127.0.0.1:8020}"

# 0. journeys exist and target the PUBLIC API (real HTTP calls, not internal imports)
[[ -d tests/journeys ]] || fail "tests/journeys/ missing"
grep -rlE "chat/completions" tests/journeys/ --include='*.py' 2>/dev/null | grep -q . \
  || fail "no journey calls /v1/chat/completions"
if grep -rqE "^\s*from services\.|^\s*import services\." tests/journeys/ --include='*.py' 2>/dev/null; then
  fail "journeys import internal services: they must exercise the PUBLIC API only"
fi
pass "journeys present and driven through the public chat API"

# 1. LIVE PROBE by the gate itself (layer a). Only when the stack is reachable.
#    In CI the stack is not up; the probe is skipped and the cassette-based report rules.
if curl -sf -m 5 "$API/v1/models" >/dev/null 2>&1; then
  REPLY=$(curl -s -m 90 "$API/v1/chat/completions" -H "Content-Type: application/json" \
    -d '{"model":"atlas","messages":[{"role":"user","content":"Réponds en une phrase : quelle est la capitale de la France ?"}]}' \
    | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); print(d["choices"][0]["message"]["content"])
except Exception as e:
    print("PARSE_ERROR", e)')
  [[ "$REPLY" != PARSE_ERROR* && -n "$REPLY" ]] || fail "live probe: no usable answer from the public API ($REPLY)"
  echo "$REPLY" | grep -qi "paris" || fail "live probe: answer does not mention Paris: $REPLY"
  # language check: French question -> French reply (function-word ratio, code excluded)
  python3 - "$REPLY" << 'PY' || fail "live probe: reply not in the question's language (French)"
import sys,re
t=re.sub(r"```.*?```","",sys.argv[1],flags=re.S).lower()
fr=len(re.findall(r"\b(la|le|les|de|des|est|et|une|un|du|en)\b",t))
en=len(re.findall(r"\b(the|is|of|and|a|an|to|in|it)\b",t))
sys.exit(0 if fr>=en else 1)
PY
  pass "live probe: public API answers correctly, in French"
else
  echo "  (stack not reachable at $API — live probe skipped, CI mode: cassette report rules)" >&2
fi

# 2. run the journeys and read the report (layer b)
rm -f BRAIN/eval/journeys.json
make test-journeys >&2 2>/dev/null || fail "make test-journeys failed"
R="BRAIN/eval/journeys.json"; [[ -f "$R" ]] || fail "report $R not produced"

python3 - "$R" << 'PY'
import sys, json
required = {
 "J1_plain_answer":        "plain question returned no usable final answer",
 "J1_language_match":      "answer language does not match the question language",
 "J2_citation_resolvable": "corpus answer has no resolvable citation",
 "J3_image_described":     "sent image was not described",
 "J3_language_match":      "image description is not in the conversation language",
 "J4_image_returned":      "image generation did not return an image",
 "J5_memory_shared":       "fact from conversation A not reused in conversation B (same project)",
 "J5_projects_isolated":   "context leaked between projects",
 "J6_tools_used":          "web question did not trigger tools",
 "J6_final_answer":        "web question ran tools but delivered NO final answer",
 "J7_auxiliary_ok":        "auxiliary calls (title/tags/follow-up) errored (or not explicitly disabled)",
}
try:
    d = json.loads(open(sys.argv[1]).read())
except Exception as e:
    print(f"::error::verify-m17: report is not valid JSON ({e})"); sys.exit(1)
if not isinstance(d, dict):
    print("::error::verify-m17: report must be a JSON object"); sys.exit(1)
missing = [k for k in required if k not in d]
if missing:
    print(f"::error::verify-m17: report does not cover: {missing}"); sys.exit(1)
failed = [f"{k}: {m}" for k, m in required.items() if d.get(k) is not True]
if failed:
    print("::error::verify-m17: user journeys failed:")
    for f in failed: print(f"  - {f}")
    sys.exit(1)
# the report must say how J4/J6 were obtained; replay is fine in CI, but must be declared
mode = d.get("mode")
if mode not in ("live", "replay"):
    print("::error::verify-m17: report must declare mode = live|replay for J4/J6"); sys.exit(1)
print(f"  ✓ all 11 journey assertions passed (mode: {mode})", file=sys.stderr)
PY

echo "== verify-m17 OK (user journeys pass through the public chat API) ==" >&2
