#!/usr/bin/env bash
# verify-m25 — intellectual disposition, not test-case patches. PROTECTED (CODEOWNERS).
#
# Enforces the generality rule mechanically: no corpus vocabulary may appear in any
# system prompt. Anything that names a case is a patch, and a patch is a gate failure.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m25: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m25 ==" >&2
P="prompts/chat.txt"
[[ -f "$P" ]] || fail "$P missing"

# ---- D1: size and disposition ----
SZ=$(wc -c < "$P")
[[ "$SZ" -le 3500 ]] || fail "$P is $SZ bytes (> 3500): the case patches were not removed"
# Compare on a whitespace-normalised copy: the disposition may be wrapped at any width.
FLAT=$(tr '\n' ' ' < "$P" | tr -s ' ')
for phrase in "who tried it" "a name, a date, a figure" "name the tension" "where a figure comes from" \
              "choose the form" "what you do not know" "what remains open"; do
  case "${FLAT,,}" in *"${phrase,,}"*) ;; *) fail "disposition text missing: \"$phrase\"";; esac
done
pass "$SZ bytes, disposition present"

# ---- D1/D2: no case vocabulary anywhere in prompts/ ----
# Fixed list: the terms of the known corpus cases. Extended dynamically from the public
# corpus when it is present on this machine.
# One term per line. Multi-word terms are matched as phrases, single words as whole words
# (so "paris" does not match "comparison" and "de" matches nothing on its own).
BANNED_FILE=$(mktemp)
cat > "$BANNED_FILE" << 'EOF'
z80
outi
otir
amstrad
gate array
t-state
solargraph
solargraphy
pinhole
ilford
spacex
ipo
nasdaq
offer price
opening price
siège de paris
castor
pollux
fretboard
guitar
rgpd
gdpr
nordia
watt-hour
jsonl
cached_input_tokens
h100
deepseek-v4
glm-5
qwen3
EOF
# No dynamic extraction: it kept promoting ordinary words (content, python, path, data)
# to banned terms. The list above is fixed and reviewed by the owner; extend it when a new
# corpus case introduces a distinctive proper noun or identifier.
HITS=""
for f in prompts/*.txt; do
  while IFS= read -r t; do
    [[ -z "$t" ]] && continue
    if [[ "$t" == *" "* ]]; then
      grep -qiF -- "$t" "$f" && HITS="$HITS $(basename $f):'$t'"
    else
      grep -qiwF -- "$t" "$f" && HITS="$HITS $(basename $f):$t"
    fi
  done < "$BANNED_FILE"
done
rm -f "$BANNED_FILE"
[[ -z "$HITS" ]] || fail "corpus vocabulary in system prompts (case patches):$HITS"
pass "no corpus vocabulary in any prompt"

# ---- D2: policy written ----
[[ -f docs/prompt-policy.md ]] || fail "docs/prompt-policy.md missing"
grep -qiE "three unrelated domains|trois domaines" docs/prompt-policy.md \
  || fail "prompt-policy.md does not state the three-domain generality test"
grep -qiE "never the case itself|jamais le cas" docs/prompt-policy.md \
  || fail "prompt-policy.md does not forbid case-specific fixes"
pass "prompt policy states the generality test"

# ---- D3: held-out set protected ----
grep -qE "^private/?$|^/private/?$" .gitignore 2>/dev/null || fail "private/ is not in .gitignore: the held-out set could leak into the repository"
grep -rq "heldout" services/ prompts/ tests/ 2>/dev/null && fail "the held-out directory is referenced from the repository: the agent must not know it"
grep -q "\-\-cases" /opt/atlas-src/corpus/run_corpus.py 2>/dev/null || [[ ! -f /opt/atlas-src/corpus/run_corpus.py ]] \
  || fail "run_corpus.py has no --cases option for the held-out set"
pass "held-out set isolated from the repository"

# ---- D4: conversation limit in tokens against the window ----
grep -qE "> *32000|> *600000|max_length=32000|max_length=200000|max_length=600000" packages/images.py \
  && fail "conversation length is still a character constant in packages/images.py"
grep -rqiE "context_tokens|context_window|window" packages/images.py services/orchestrator/chat_schema.py 2>/dev/null \
  || fail "conversation length is not compared with the model context window"
grep -rqiE "value_error\"|\"value_error" services/orchestrator/chat_api.py 2>/dev/null \
  && fail "chat_api still surfaces bare value_error to the user"
pass "conversation length measured in tokens against the window"

# ---- D5: limits inventory ----
[[ -f docs/limits.md ]] || fail "docs/limits.md missing"
for k in "2000|2 000" "400" "32000|32 000" "6 MB|6MB|6291456"; do
  grep -qE "($k)" docs/limits.md || fail "docs/limits.md does not account for the inherited limit matching '$k'"
done
grep -qiE "justif|leftover|removed" docs/limits.md || fail "docs/limits.md does not classify each limit as justified or leftover"
pass "limits inventory present"

# ---- journeys ----
rm -f BRAIN/eval/journeys.json
make test-journeys >&2 2>/dev/null || fail "make test-journeys failed"
J="BRAIN/eval/journeys.json"; [[ -f "$J" ]] || fail "report $J not produced"
python3 - "$J" << 'PY'
import sys, json
new = {
 "J50_prompt_clean":"prompt still carries corpus vocabulary or exceeds 3500 bytes",
 "J51_fresh_design_question":"fresh open question: fewer than three dated precedents, no stated tension, or no conclusion",
 "J52_long_conversation":"a 40k-character conversation was rejected, or the window message lacks measured figures",
 "J53_corpus_no_regression":"public corpus mean regressed by more than 0.5 after removing the patches",
}
try: d=json.loads(open(sys.argv[1]).read())
except Exception as e: print(f"::error::verify-m25: report invalid JSON ({e})"); sys.exit(1)
# J34 (SpaceX offer vs opening price) is an ACCEPTED regression: it passed only because a
# sentence written for that case sat in the system prompt. Removing the patch is the point
# of this milestone, and the loss is recorded in reports/M25.md. Any OTHER earlier journey
# that regresses still fails the gate.
ACCEPTED_REGRESSIONS = {"J34_spacex_search_first"}
prev=[k for k in d if k.startswith("J") and k[1:3].isdigit() and int(k[1:3])<50]
bad=[k for k in prev if d.get(k) is not True and k not in ACCEPTED_REGRESSIONS]
if bad: print("::error::verify-m25: regression on earlier journeys:", bad); sys.exit(1)
for k in ACCEPTED_REGRESSIONS:
    if d.get(k) is not True:
        import pathlib
        rep = pathlib.Path("reports/M25.md")
        if not rep.exists() or "regression caused by removing a case patch" not in rep.read_text():
            print(f"::error::verify-m25: {k} fails and reports/M25.md does not document it "
                  "under 'regression caused by removing a case patch'")
            sys.exit(1)
miss=[k for k in new if k not in d]
if miss: print(f"::error::verify-m25: report does not cover: {miss}"); sys.exit(1)
fails=[f"{k}: {m}" for k,m in new.items() if d.get(k) is not True]
if fails:
    print("::error::verify-m25: journeys failed:")
    for f in fails: print("  -", f)
    sys.exit(1)
for k in ("j51_precedents_named","j53_means_before","j53_means_after"):
    if k not in d: print(f"::error::verify-m25: report must record {k}"); sys.exit(1)
if int(d["j51_precedents_named"]) < 3:
    print(f"::error::verify-m25: J51 named only {d['j51_precedents_named']} precedents"); sys.exit(1)
if d.get("mode") not in ("live","replay"):
    print("::error::verify-m25: report must declare mode = live|replay"); sys.exit(1)
print(f"  ✓ journeys passed; J51 named {d['j51_precedents_named']} precedents; "
      f"corpus {d['j53_means_before']} -> {d['j53_means_after']} (mode: {d.get('mode')})", file=sys.stderr)
PY
echo "== verify-m25 OK ==" >&2
