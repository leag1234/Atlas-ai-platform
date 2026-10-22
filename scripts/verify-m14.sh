#!/usr/bin/env bash
# verify-m14 — client MCP : lecture + action confirmée sur les outils. PROTÉGÉ.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m14: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m14 ==" >&2

grep -rqiE "mcp" services/orchestrator/ 2>/dev/null || fail "client MCP absent du harness"
pass "client MCP présent"
# les actions à effet de bord exigent une confirmation
grep -rqiE "confirm" services/orchestrator/ 2>/dev/null || fail "aucun mécanisme de confirmation pour les actions à effet de bord"
pass "confirmation des actions présente"

if ! make test-mcp >&2 2>/dev/null; then fail "make test-mcp échoue"; fi
R="BRAIN/eval/mcp.json"; [[ -f "$R" ]] || fail "rapport mcp absent"
python3 - "$R" << 'PYEOF'
import sys, json
d = json.load(open(sys.argv[1]))
for k, msg in {
  "read_ok": "lecture d'une ressource via MCP KO",
  "write_after_confirm_ok": "action après confirmation KO",
  "write_without_confirm_refused": "une action NON confirmée n'a pas été refusée (grave)",
  "no_secret_leak": "un secret MCP a fuité dans une réponse/log",
}.items():
    if not d.get(k):
        print(f"::error::verify-m14: {msg}"); sys.exit(1)
print("  ✓ lecture, action confirmée, refus sans confirmation, pas de fuite", file=sys.stderr)
PYEOF
echo "== verify-m14 OK ==" >&2
