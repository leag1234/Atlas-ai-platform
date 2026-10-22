#!/usr/bin/env bash
# verify-m3 — Harness agentique + outils web. PROTÉGÉ (CODEOWNERS).
#
# Vérifie la boucle d'outils bornée et la sécurité des outils web.
# Preuve sur la VM (accès SerpApi) ; en CI, contrôle statique + tests unitaires
# qui n'appellent pas le web réel (cassettes).
#
# Critères (MISSION.md M3) :
#   - tool-calling : POC-E4 >= 90% (via make eval-tools -> BRAIN/eval/tools.json)
#   - sécurité web : SSRF (IP privées refusées), robots.txt respecté, plafond de fetches
#   - budgets durs : les 4 budgets (tokens, tool_calls, wall_clock, cost) arrêtent proprement
#   - POC-E6 (web Q/R) exécutable de bout en bout
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m3: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m3 ==" >&2

# 1. l'orchestrateur/harness existe
[[ -d services/orchestrator || -d services/harness ]] || fail "service orchestrateur/harness absent"
pass "service harness présent"

# 2. les outils sont déclarés (web_search, web_fetch, rag_search, calculator)
TOOLS_OK=1
for t in web_search web_fetch rag_search calculator; do
  grep -rq "$t" services/ 2>/dev/null || TOOLS_OK=0
done
[[ "$TOOLS_OK" == "1" ]] || fail "un ou plusieurs outils manquants (web_search/web_fetch/rag_search/calculator)"
pass "les 4 outils sont déclarés"

# 3. tests de sécurité web (SSRF, robots, plafond) — doivent exister et passer
if ! make test-web-security >&2 2>/dev/null; then
  fail "tests de sécurité web échouent (SSRF/robots.txt/plafond de fetches)"
fi
pass "sécurité web OK (SSRF refusé, robots.txt respecté, plafond appliqué)"

# 4. tests des budgets durs (les 4 budgets arrêtent proprement)
if ! make test-budgets >&2 2>/dev/null; then
  fail "tests des budgets durs échouent (tokens/tool_calls/wall_clock/cost)"
fi
pass "budgets durs OK (arrêt propre sur chaque limite)"

# 5. tool-calling E4 >= 90%
if ! make eval-tools >&2 2>/dev/null; then
  fail "make eval-tools a échoué"
fi
REPORT="BRAIN/eval/tools.json"
[[ -f "$REPORT" ]] || fail "rapport $REPORT absent"
SCORE=$(python3 -c "import json;print(json.load(open('$REPORT')).get('success_rate','NA'))" 2>/dev/null || echo NA)
[[ "$SCORE" != "NA" ]] || fail "champ success_rate absent du rapport"
OK=$(python3 -c "print(1 if float('$SCORE') >= 0.90 else 0)" 2>/dev/null || echo 0)
[[ "$OK" == "1" ]] || fail "tool_success_rate = $SCORE < 0.90"
pass "tool-calling E4 = $SCORE (>= 0.90)"

# 6. E6 (web Q/R) exécutable — au moins un cas passe de bout en bout
if ! make eval-web >&2 2>/dev/null; then
  fail "make eval-web (POC-E6) n'est pas exécutable"
fi
pass "E6 (web Q/R) exécutable de bout en bout"

echo "== verify-m3 OK (outils, sécurité web, budgets, E4=$SCORE, E6) ==" >&2
