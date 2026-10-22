#!/usr/bin/env bash
# verify-m23 — readable rendering. PROTECTED (CODEOWNERS).
# A gate cannot judge aesthetics; it checks what is objectively checkable and leaves the
# visual judgement to the owner on screen.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m23: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m23 ==" >&2
CSS="services/orchestrator/chat-ui.css"
[[ -f "$CSS" ]] || fail "$CSS missing"

# --- custom properties, used through var() ---
for v in --atlas-body-font --atlas-body-size --atlas-line-height; do
  # The property must be DEFINED (name followed by a colon), not merely referenced
  # inside a var(): a missing definition makes every var() fall back silently.
  grep -qE -- "$v\s*:" "$CSS" || fail "custom property $v is not defined"
done
grep -qE "font-family:\s*var\(--atlas-body-font" "$CSS" \
  || fail "the body font is not applied through var(--atlas-body-font)"
pass "custom properties defined and applied through var()"

# --- the font must stay overridable by a user preference ---
grep -nE "font-family[^;]*!important" "$CSS" \
  && fail "!important on font-family: a user preference could no longer override it"
pass "no !important on font-family"

# --- code rendering ---
grep -qE "(^|[^-a-z])pre\b" "$CSS" || fail "no rule targeting <pre> (code blocks)"
grep -qE "(^|[^-a-z])code\b" "$CSS" || fail "no rule targeting <code> (inline code)"
grep -qiE "line-?number|linenumber" "$CSS" || fail "line numbers are not handled"
grep -qiE "hover" "$CSS" || fail "no hover rule: the copy affordance must appear on hover only"
pass "code blocks and inline code addressed"

# --- readability: column width and spacing ---
grep -qE "max-width:\s*(6[5-9][0-9]|7[0-2][0-9])px|max-width:\s*4[0-6]rem|--atlas-column" "$CSS" \
  || fail "no text column cap around 680-720px"
grep -qE "(margin|padding)[^;]*(h2|h3)|h[23][^{]*\{[^}]*margin" "$CSS" \
  || grep -qE "--atlas-section-space" "$CSS" \
  || fail "no section spacing rule"
pass "column width and section spacing set"

# --- dark mode parity ---
DARK=$(grep -c "\.dark" "$CSS" || true)
[[ "$DARK" -ge 3 ]] || fail "only $DARK dark-mode rules: each new rule needs a dark counterpart"
pass "$DARK dark-mode rules present"

# --- documentation: every rule block commented, fragile selectors listed ---
COMMENTS=$(grep -c "/\*" "$CSS" || true)
BLOCKS=$(grep -c "{" "$CSS" || true)
[[ "$COMMENTS" -ge $(( BLOCKS / 2 )) ]] \
  || fail "only $COMMENTS comments for $BLOCKS rule blocks: each block must state what it targets"
pass "$COMMENTS comments for $BLOCKS blocks"

[[ -f reports/M23.md ]] || fail "reports/M23.md missing"
grep -qiE "variable|custom propert" reports/M23.md || fail "reports/M23.md does not list the theme variables found in the running UI"
grep -qiE "fragile|upgrade|version" reports/M23.md || fail "reports/M23.md does not identify version-fragile selectors and how to repair them"
pass "report documents theme variables and fragile selectors"

# --- the M21 green user message must survive ---
grep -q "user-message" "$CSS" || fail "the green user-message styling from M21 was removed"
pass "M21 user-message styling preserved"


# --- D6: generated code must be in English whatever the conversation language ---
for f in prompts/chat.txt prompts/chat-agent.txt; do
  [[ -f "$f" ]] || continue
  grep -qiE "identifiers?.*English|English.*(identifiers?|comments?|docstrings?)|code (in|is always) English" "$f" \
    || fail "$f does not state that generated code (identifiers, comments, docstrings) must be English"
done
grep -rqiE "user.s language|langue de l|displayed to (end )?users?|visible labels" prompts/chat.txt prompts/chat-agent.txt 2>/dev/null \
  || fail "the rule does not distinguish code (English) from prose and user-visible strings"
pass "English-code rule stated in the prompts"


# --- D7: the activity indicator must fire on request start, not only on tool events ---
CS="services/orchestrator/chat_stream.py"
[[ -f "$CS" ]] || fail "$CS missing"
grep -qiE "start|initial|first_state|on_request" "$CS" \
  || fail "no activity state emitted at request start: a request without tool calls shows nothing"
grep -qiE "réflexion|thinking|rédaction|generating" "$CS" \
  || fail "no phase label for plain generation (thinking / writing)"
pass "activity indicator emitted at request start, not only on tool events"


# --- D8: attached documents must be read in full, not sampled ---
IS="services/orchestrator/image_store.py"
# An actual extractor must exist: a function that turns a PDF/DOCX/text file into text.
grep -rqE "def (extract|read_document|extract_text)" services/ packages/ 2>/dev/null \
  || fail "no document extractor function: attached PDFs are never read (only images are handled)"
grep -rqiE "pdf" services/ packages/ --include="*.py" 2>/dev/null \
  || fail "no PDF handling anywhere in the services"
# the upload path must handle more than image_url
grep -rqiE "image_url" "$IS" 2>/dev/null && {
  grep -rqiE "(file|document|attachment)" "$IS" services/orchestrator/document*.py 2>/dev/null \
    || fail "$IS still handles images only: add a document branch"
}
# an attached document must not be answered from the corpus RAG
grep -rqiE "attach|uploaded_document|not.*corpus|skip.*rag" services/orchestrator/chat_pipeline.py 2>/dev/null \
  || fail "nothing prevents an attached document from being answered via the corpus RAG"
pass "document path present and separated from the corpus RAG"

echo "== verify-m23 OK (visual judgement remains the owner's) ==" >&2
