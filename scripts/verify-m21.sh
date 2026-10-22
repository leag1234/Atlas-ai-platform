#!/usr/bin/env bash
# verify-m21 — close the quality gap: reasoning, expertise, full context, clear UI. PROTECTED.
# Static locks on the four measured causes, a live probe on the exact case that failed,
# then the journeys report.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m21: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m21 ==" >&2
API="${ATLAS_CHAT_API:-http://127.0.0.1:8020}"

# ---- (a) the four measured causes must be gone ----
grep -qE "\[:2000\]" services/orchestrator/tools.py 2>/dev/null \
  && fail "web page text is still cut to 2000 bytes (tools.py) — deliver whole text or synthesise"
grep -qE "\[:400\]" services/retrieval/tool.py 2>/dev/null \
  && fail "RAG chunks are still cut to 400 bytes (retrieval/tool.py)"
pass "no byte-level truncation of web text or RAG chunks"

grep -qiE "describe only what is visible" prompts/vision.txt 2>/dev/null \
  && fail "vision prompt still asks for description instead of expertise"
for f in prompts/chat.txt prompts/vision.txt; do
  grep -qiE "expert|derivation|show (the|your) (calculation|work)|conclude" "$f" 2>/dev/null \
    || fail "$f does not ask for expert answers with derivation and conclusion"
done
pass "prompts ask for expertise, derivation and an explicit conclusion"

# Model selector (deep mode was abandoned: measured unusable on all three provider models
# on 2026-09-17 — see reports/M21.md). What must be exposed now is a choice of MODELS.
# Profiles are named after the actual models (owner decision 2026-09-17).
for m in "atlas-qwen" "atlas-glm" "atlas-deepseek"; do
  grep -rqE "$m" services/orchestrator/ services/model-gateway/ 2>/dev/null \
    || fail "model $m is not exposed: the selector must offer the configured provider models"
done
# "atlas-deep" must not match "atlas-deepseek": \b is not portable in ERE, so we
# explicitly require that no alphanumeric character follows.
grep -rqE "atlas-deep([^a-zA-Z0-9-]|$)" services/orchestrator/ services/model-gateway/ 2>/dev/null \
  && fail "atlas-deep is still exposed: deep mode was abandoned with measured evidence"
# reasoning_effort must be sent EXPLICITLY on every call: the provider default changed
# mid-day on 2026-09-17 and silently emptied every answer.
grep -rqE "reasoning_effort" services/model-gateway/ services/orchestrator/ 2>/dev/null \
  || fail "reasoning_effort is never sent to the provider (never rely on its default)"
pass "model selector exposed; reasoning_effort always sent explicitly"

# empty-answer guard
# Provider replies are untrusted data: empty content, text in an unexpected field,
# finish_reason length, malformed JSON — all must degrade gracefully, never silently.
grep -rqiE "empty.*content|content.*empty|finish_reason" services/orchestrator/ services/model-gateway/ 2>/dev/null \
  || fail "no validation of provider replies (empty content, finish_reason, malformed body)"
pass "provider replies are validated, not assumed"

# images by reference for uploads
grep -rqE "image_store" services/orchestrator/chat_pipeline.py services/orchestrator/vision.py services/orchestrator/chat_api.py 2>/dev/null \
  || fail "uploaded images are not stored by reference (image_store not used on the upload path)"
pass "uploaded images handled by reference"

# UI: tool steps named
grep -rqE "recherche web|lecture de page|web_search|web_fetch" services/orchestrator/chat_stream.py services/orchestrator/labels* prompts/*labels* 2>/dev/null \
  || fail "tool steps are not named in the UI stream"
pass "tool steps are named"

# UI: live activity indicator (deep mode can think for a minute before any text)
grep -rqiE "Réflexion|thinking|activity|elapsed" services/orchestrator/chat_stream.py 2>/dev/null \
  || fail "no live activity indicator streamed (user sees a frozen screen while the model reasons)"
pass "live activity indicator streamed"

# ---- (b) live probe: the National Geographic case, verbatim ----
if curl -sf -m 5 "$API/v1/models" >/dev/null 2>&1; then
  Q='lis ça https://www.nationalgeographic.com/premium/article/longest-known-exposure-pinhole-uk et dis moi comment fabriquer ce type de "camera"'
  OUT=$(curl -s -m 240 "$API/v1/chat/completions" -H "Content-Type: application/json" \
    -d "$(python3 -c 'import json,sys;print(json.dumps({"model":"atlas","messages":[{"role":"user","content":sys.argv[1]}]}))' "$Q")" \
    | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["choices"][0]["message"]["content"])
except Exception as e: print("PARSE_ERROR",e)')
  [[ "$OUT" != PARSE_ERROR* && -n "$OUT" ]] || fail "live probe: no answer on the National Geographic case"
  HITS=$(echo "$OUT" | grep -oiE "ilford|bayfordbury|cidre|cider|multigrade" | tr '[:upper:]' '[:lower:]' | sort -u | wc -l)
  [[ "$HITS" -ge 3 ]] || fail "live probe: only $HITS/4 article details reached the answer (page still truncated?)"
  echo "$OUT" | grep -qiE "tronqu|truncat" && fail "live probe: the answer still reports truncated source text"
  pass "live probe: web article fully ingested ($HITS/4 details present)"
else
  echo "  (stack unreachable at $API — live probe skipped; CI mode)" >&2
fi

# ---- (c) journeys ----
rm -f BRAIN/eval/journeys.json
make test-journeys >&2 2>/dev/null || fail "make test-journeys failed"
R="BRAIN/eval/journeys.json"; [[ -f "$R" ]] || fail "report $R not produced"
python3 - "$R" << 'PY'
import sys, json
new = {
 "J27_model_selector":"the same question does not run on the three exposed models",
 "J28_reply_validation":"a malformed provider reply is not handled gracefully",
 "J29_expert_chord":"chord photo not answered as an expert (name, tablature, notes, conclusion)",
 "J30_full_article":"National Geographic article details did not reach the answer",
 "J31_rag_beyond_400":"RAG fact located beyond byte 400 of its chunk was not answered",
 "J32_tool_steps_named":"tool steps are not named / URLs not listed",
 "J33_activity_indicator":"no activity indicator within 2 s or not updated during reasoning",
}
try: d=json.loads(open(sys.argv[1]).read())
except Exception as e: print(f"::error::verify-m21: report invalid JSON ({e})"); sys.exit(1)
prev=[k for k in d if k.startswith("J") and k[1:3].isdigit() and int(k[1:3])<27]
bad=[k for k in prev if d.get(k) is not True]
if bad: print("::error::verify-m21: regression on earlier journeys:", bad); sys.exit(1)
miss=[k for k in new if k not in d]
if miss: print(f"::error::verify-m21: report does not cover: {miss}"); sys.exit(1)
fails=[f"{k}: {m}" for k,m in new.items() if d.get(k) is not True]
if fails:
    print("::error::verify-m21: journeys failed:")
    for f in fails: print("  -", f)
    sys.exit(1)
for k in ("j27_models","j27_costs_eur"):
    if k not in d: print(f"::error::verify-m21: J27 must record {k}"); sys.exit(1)
if not isinstance(d.get("j27_models"), list) or len(d["j27_models"]) < 3:
    print("::error::verify-m21: J27 must record the three models it compared"); sys.exit(1)
if d.get("mode") not in ("live","replay"):
    print("::error::verify-m21: report must declare mode = live|replay"); sys.exit(1)
print(f"  ✓ journeys passed, no regression (mode: {d.get('mode')})", file=sys.stderr)
PY
echo "== verify-m21 OK ==" >&2
