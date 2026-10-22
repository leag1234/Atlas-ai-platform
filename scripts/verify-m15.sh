#!/usr/bin/env bash
# verify-m15 — API OpenAI-compatible durcie (auth, quotas, tools, streaming). PROTÉGÉ.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m15: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m15 ==" >&2

grep -rqiE "api.?key|bearer|authoriz" services/orchestrator/ 2>/dev/null || fail "authentification par clé API absente"
pass "authentification par clé présente"

if ! make test-devapi >&2 2>/dev/null; then fail "make test-devapi échoue"; fi
R="BRAIN/eval/devapi.json"; [[ -f "$R" ]] || fail "rapport devapi absent"
python3 - "$R" << 'PYEOF'
import sys, json
d = json.load(open(sys.argv[1]))
for k, msg in {
  "invalid_key_refused": "une clé invalide n'a pas été refusée",
  "quota_enforced": "les quotas par clé ne sont pas appliqués",
  "tool_calls_ok": "function calling (tool_calls) non conforme OpenAI",
  "streaming_ok": "streaming SSE non conforme",
  "large_context_ok": "grand contexte (dépôt de code) non supporté",
  "cost_per_key_tracked": "coût par clé non tracé",
}.items():
    if not d.get(k):
        print(f"::error::verify-m15: {msg}"); sys.exit(1)
print("  ✓ auth, quotas, tool_calls, streaming, grand contexte, coût par clé OK", file=sys.stderr)
PYEOF
# test d'intégration réel avec un client OpenAI-compatible (Codex/Claude Code simulés par le SDK)
if [[ -n "${SCW_GENERATIVE_API_KEY:-}" ]]; then
  make test-devapi-e2e >&2 2>/dev/null || fail "test bout-en-bout client OpenAI (Codex/Claude Code) échoue"
  pass "client OpenAI réel (type Codex/Claude Code) réalise une tâche de bout en bout"
fi
echo "== verify-m15 OK ==" >&2
