#!/usr/bin/env bash
# verify-m10 — grandes entrées gérées par synthèse (jamais troncature-excuse). PROTÉGÉ.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m10: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m10 ==" >&2

# un mécanisme de synthèse/retrieval sur grand contenu existe
grep -rqiE "synthes|summar|chunk|hierarch" services/ 2>/dev/null || fail "aucun mécanisme de synthèse/chunking pour grandes entrées"
pass "mécanisme de synthèse présent"

# test : une grande entrée produit une réponse utile SANS erreur de contexte
if ! make test-large-input >&2 2>/dev/null; then
  fail "make test-large-input échoue"
fi
R="BRAIN/eval/large-input.json"; [[ -f "$R" ]] || fail "rapport large-input absent"
OK=$(python3 -c "import json;d=json.load(open('$R'));print(d.get('answered') and not d.get('context_exceeded'))" 2>/dev/null||echo False)
[[ "$OK" == "True" ]] || fail "grande entrée : réponse non produite ou context_exceeded (doit synthétiser, pas rejeter)"
pass "grande entrée synthétisée et répondue (pas de troncature-excuse)"
echo "== verify-m10 OK ==" >&2
