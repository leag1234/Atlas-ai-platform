#!/usr/bin/env bash
# verify-m24 — documents and files through Open Terminal. PROTECTED (CODEOWNERS).
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m24: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m24 ==" >&2

# --- D1: the terminal is part of the stack, started by serve ---
S="services/orchestrator/serving.py"
grep -q "open-terminal" "$S" || fail "$S does not start the Open Terminal container: make serve must bring it up"
grep -qE "127\.0\.0\.1:8000|127\.0\.0\.1::8000" "$S" || fail "the terminal port is not bound to 127.0.0.1 (it must not be publicly exposed)"
grep -qE "OPEN_TERMINAL_API_KEY" "$S" infra/*.env 2>/dev/null || fail "OPEN_TERMINAL_API_KEY is not read from the environment"
pass "terminal started by serve, bound locally, key from the environment"

# --- D1: Open WebUI is pointed at it, with filesystem uploads ---
grep -q "TERMINAL_SERVER_CONNECTIONS" infra/chat-ui.env || fail "TERMINAL_SERVER_CONNECTIONS is not configured in infra/chat-ui.env"
grep -q "filesystem" infra/chat-ui.env || fail "chat uploads are not set to 'filesystem': attachments would still be reduced to RAG fragments"
grep -qE "localhost:8000" infra/chat-ui.env \
  && fail "the terminal URL uses localhost: containers must reach each other by service name"
pass "Open WebUI points at the terminal with filesystem uploads"

# --- D2: containment documented and enforced ---
[[ -f runbooks/files.md ]] || fail "runbooks/files.md missing: the containment must be documented"
grep -qiE "cannot reach|no access|isolat|contain" runbooks/files.md || fail "runbooks/files.md does not state what the terminal cannot reach"
grep -qiE "shell|exposure|surface" runbooks/files.md || fail "runbooks/files.md does not state that shell access is a new exposure surface"
grep -qE "/opt/atlas-src/repo|atlas-src.*:/" "$S" | grep -i "terminal" \
  && fail "the ATLAS repository appears mounted into the terminal container"
pass "containment documented and the repository is not mounted"

# --- D3/D5: format coverage and the two transformation strategies ---
for fmt in docx xlsx pptx odt ods pdf; do
  grep -rqi "$fmt" prompts/ runbooks/files.md 2>/dev/null \
    || fail "format $fmt is not covered in the prompts or the runbook"
done
grep -rqiE "regenerat|edit in place|en place" prompts/ runbooks/files.md 2>/dev/null \
  || fail "the two transformation strategies (regenerate vs edit in place) are not stated"
grep -rqiE "say which|state whether|indique|précise" prompts/ runbooks/files.md 2>/dev/null \
  || fail "nothing requires the model to say which strategy it used"
pass "formats covered; transformation strategies stated and announced"

# --- D3: extraction failures reported, not hidden ---
grep -rqiE "scanned|password|protected|no text layer|extraction fail" prompts/ services/ 2>/dev/null \
  || fail "extraction failures (scanned or protected PDF) are not handled explicitly"
pass "extraction failures reported explicitly"


# --- D6: the CSS must target the classes Open WebUI actually renders ---
CSS="services/orchestrator/chat-ui.css"
[[ -f "$CSS" ]] || fail "$CSS missing"
for c in copy-code-button nb-code; do
  grep -q "$c" "$CSS" || fail "the stylesheet does not target '$c': M23 targeted .markdown, which matches nothing"
done
grep -qE "svelte-[a-z0-9]{6,}" "$CSS" \
  && fail "a generated Svelte suffix is hardcoded in the CSS: it changes at every build"
pass "CSS targets the real Open WebUI classes, no generated suffix hardcoded"

# --- D6/D7: the rendering change must be PROVEN, not asserted ---
[[ -f reports/M24.md ]] || fail "reports/M24.md missing"
SHOTS=$(find reports -maxdepth 1 -name "m24-*.png" 2>/dev/null | wc -l)
[[ "$SHOTS" -ge 2 ]] || fail "only $SHOTS screenshot(s) in reports/: before/after proof is required for the rendering"
grep -qiE "before|after|avant|après" reports/M24.md || fail "reports/M24.md does not present a before/after comparison"
grep -qiE "activity|indicator|indicateur" reports/M24.md || fail "reports/M24.md says nothing about the activity indicator"
pass "$SHOTS screenshots and a before/after report"

# --- D8: attachment ceiling and Tavily ---
grep -rqE "0\.30|0,30" services/ contracts/ 2>/dev/null \
  || fail "the 0.30 EUR attachment ceiling is not applied anywhere"
grep -rqi "tavily" services/ 2>/dev/null || fail "Tavily is not wired as a search provider"
grep -rq "TAVILY_API_KEY" services/ infra/ 2>/dev/null || fail "TAVILY_API_KEY is not read from the environment"
grep -rqiE "serpapi" services/ 2>/dev/null || fail "SerpApi must remain available as a fallback provider"
# a quota failure must be visible to the user, not silently absorbed
grep -rqiE "quota|search_unavailable|provider.*fail" services/orchestrator/ 2>/dev/null \
  || fail "an exhausted search quota is not surfaced: four corpus cases were scored on degraded searches"
pass "Tavily wired with SerpApi fallback; quota failures surfaced; 0.30 EUR ceiling applied"

# --- journeys ---
rm -f BRAIN/eval/journeys.json
make test-journeys >&2 2>/dev/null || fail "make test-journeys failed"
J="BRAIN/eval/journeys.json"; [[ -f "$J" ]] || fail "report $J not produced"
python3 - "$J" << 'PY'
import sys, json
new = {
 "J42_pptx_and_pdf":"the deck was not returned as a downloadable pptx AND pdf",
 "J43_docx_fixed":"the corrected .docx was not returned, or the strategy was not stated",
 "J44_xlsx_sheet_chart":"the new sheet with its chart was not produced, or the figures are wrong",
 "J45_long_pdf_synthesis":"the long PDF synthesis does not cover the whole document",
 "J46_extraction_failure":"a protected or scanned PDF did not produce an explicit failure",
 "J47_large_attachment":"a ~180k-token PDF was refused instead of synthesised",
 "J48_cpc_search":"the CPC timing question did not search, or did not admit an exhausted quota",
 "J49_rendering_proof":"code blocks still show line numbers/toolbar, or body text is not serif",
}
try: d=json.loads(open(sys.argv[1]).read())
except Exception as e: print(f"::error::verify-m24: report invalid JSON ({e})"); sys.exit(1)
ACCEPTED_REGRESSIONS = {"J34_spacex_search_first"}  # M25 owner ruling 2026-09-21
prev=[k for k in d if k.startswith("J") and k[1:3].isdigit() and int(k[1:3])<42]
bad=[k for k in prev if d.get(k) is not True and k not in ACCEPTED_REGRESSIONS]
if bad: print("::error::verify-m24: regression on earlier journeys:", bad); sys.exit(1)
miss=[k for k in new if k not in d]
if miss: print(f"::error::verify-m24: report does not cover: {miss}"); sys.exit(1)
fails=[f"{k}: {m}" for k,m in new.items() if d.get(k) is not True]
if fails:
    print("::error::verify-m24: journeys failed:")
    for f in fails: print("  -", f)
    sys.exit(1)
# per-journey evidence: which model drove the terminal, and how well
for k in ("j42_model","j42_commands","j42_failed_commands","j42_seconds"):
    if k not in d: print(f"::error::verify-m24: J42 must record {k}"); sys.exit(1)
if d.get("mode") not in ("live","replay"):
    print("::error::verify-m24: report must declare mode = live|replay"); sys.exit(1)
print(f"  ✓ journeys passed (mode: {d.get('mode')}, J42 driven by {d.get('j42_model')}: "
      f"{d.get('j42_commands')} commands, {d.get('j42_failed_commands')} failed)", file=sys.stderr)
PY
echo "== verify-m24 OK ==" >&2
