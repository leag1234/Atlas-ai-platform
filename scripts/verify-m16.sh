#!/usr/bin/env bash
# verify-m16 — English localization of the public repository. PROTECTED (CODEOWNERS).
#
# The repository is PUBLIC: all documentation and code meant for external readers
# must be in English. Some content is intentionally multilingual (test corpus,
# golden evaluation sets) and is explicitly excluded from translation.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m16: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m16 ==" >&2

# 1. README.md must exist, in English, with the expected sections.
[[ -f README.md ]] || fail "README.md missing (first thing seen on a public repository)"
grep -qiE "^#|overview|architecture" README.md || fail "README.md has no title or overview"
grep -qiE "install|quick ?start|getting started|usage" README.md || fail "README.md has no install/quickstart section"
pass "README.md present and structured"

# 2. French detection in public-facing files. We match common French function
#    words (more reliable than accents alone: technical English may quote
#    accented proper nouns). This script excludes itself (it holds the pattern).
FR='\b(le|la|les|un|une|des|du|dans|pour|avec|sans|est|sont|etre|avoir|cette|ces|qui|que|dont|ainsi|donc|mais|aucun|chaque|toujours|jamais|doit|peut|selon|lors|afin|voir|ici)\b'
SELF="scripts/verify-m16.sh"
# Protected internal check scripts (scripts/verify-*.sh) are tooling, not public
# documentation: they are excluded from the translation requirement.
EXCLUDE='^(corpus/|evals/golden/|BRAIN/|scripts/verify-m[0-9]+\.sh)'

scan() {  # $1=dir  $2=include-glob  $3=label
  [[ -d "$1" ]] || return 0
  local hits
  hits=$(grep -rilE "$FR" --include="$2" "$1" 2>/dev/null | grep -vE "$EXCLUDE" || true)
  [[ -z "$hits" ]] && return 0
  echo "::error::verify-m16: French found in $3:" >&2; echo "$hits" | head -10 >&2
  return 1
}

ERR=0
scan docs      "*.md" "docs/"      || ERR=1
scan contracts "*.md" "contracts/" || ERR=1
scan runbooks  "*.md" "runbooks/"  || ERR=1
for f in README.md MISSION.md AGENTS.md; do
  [[ -f "$f" ]] || continue
  grep -qiE "$FR" "$f" 2>/dev/null && { echo "::error::verify-m16: French found in $f" >&2; ERR=1; }
done
[[ "$ERR" == "0" ]] || fail "documentation not translated (see above)"
pass "documentation (docs/, contracts/, runbooks/, README, MISSION, AGENTS) is in English"

# 3. Code: comments, docstrings and user-facing messages must be in English.
#    We only look at COMMENT lines and docstrings, and we match French *accents*
#    there. Rationale: identifiers and keyword arguments (e.g. Pydantic's `le=2048`)
#    are not prose and must never be flagged; French prose in this codebase is
#    always accented. Files whose purpose is to test French handling are excluded.
CODE_EXCLUDE='(corpus/|evals/golden/|BRAIN/|scripts/verify-m[0-9]+\.sh|tests/.*(multiling|french|langue|i18n).*)'
HITS=""
while IFS= read -r f; do
  [[ "$f" =~ $CODE_EXCLUDE ]] && continue
  # comment lines (#, //) and docstring lines, containing French accents
  if grep -nE '^[[:space:]]*(#|//)|"""|\x27\x27\x27' "$f" 2>/dev/null \
     | grep -qE '[àâçéèêëîïôûùüœ]'; then
    HITS="$HITS$f"$'\n'
  fi
done < <(find services scripts infra tests -type f \( -name '*.py' -o -name '*.sh' \) 2>/dev/null)
HITS=$(printf '%s' "$HITS" | grep -v '^$' || true)
if [[ -n "$HITS" ]]; then
  echo "::error::verify-m16: French comments/docstrings found in code:" >&2
  echo "$HITS" | head -10 >&2
  fail "code comments not translated ($(echo "$HITS" | wc -l) files)"
fi
pass "code comments and docstrings are in English"

# 4. Intentionally multilingual content MUST be preserved (not translated away).
grep -rqE "[àéèêçù]" corpus/ 2>/dev/null || fail "multilingual corpus lost: French must REMAIN there"
grep -rqE "[àéèêçù]" evals/golden/ 2>/dev/null || fail "multilingual golden sets lost: French must REMAIN there"
pass "multilingual corpus and golden sets preserved"

echo "== verify-m16 OK (public repo in English, multilingual test content intact) ==" >&2
