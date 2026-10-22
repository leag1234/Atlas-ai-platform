#!/usr/bin/env bash
# verify-m6 — Durcissement + bench final + rapport GO/NO-GO. PROTÉGÉ (CODEOWNERS).
#
# Dernier jalon : consolide les preuves des jalons précédents en un rapport de
# décision. Ne crée PAS de GPU (réutilise les benchs/évals déjà produits).
#
# Critères (MISSION.md M6) :
#   - un rapport GO/NO-GO est généré (reports/GO-NOGO.md) avec les chiffres réels
#   - il agrège les résultats des suites d'éval (recall E1, tool E4, routage E8, etc.)
#   - un filtre d'entrée minimal (guardrail) existe
#   - `make demo` déroule un scénario de bout en bout sans erreur
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m6: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m6 ==" >&2

# 1. le rapport GO/NO-GO est généré par make report (ou make go-nogo)
rm -f reports/GO-NOGO.md
if ! make report >&2 2>/dev/null && ! make go-nogo >&2 2>/dev/null; then
  fail "aucune cible make report / make go-nogo fonctionnelle"
fi
REPORT="reports/GO-NOGO.md"
[[ -f "$REPORT" ]] || fail "rapport $REPORT non généré"
pass "rapport GO/NO-GO généré"

# 2. le rapport contient une décision explicite et des chiffres
grep -qiE "GO|NO-GO|NOGO" "$REPORT" || fail "le rapport ne contient pas de décision GO/NO-GO"
grep -qE "[0-9]" "$REPORT" || fail "le rapport ne contient aucun chiffre"
pass "rapport contient une décision et des chiffres"

# 3. le rapport agrège les suites clés (au moins recall/E1 et tool/E4 ou routage/E8)
AGG=0
for k in recall E1 E4 tool E8 routage rag; do
  grep -qiE "$k" "$REPORT" && AGG=1
done
[[ "$AGG" == "1" ]] || fail "le rapport n'agrège aucune métrique d'éval connue"
pass "rapport agrège les métriques d'éval"

# 4. filtre d'entrée minimal (guardrail) présent dans le code
grep -rqiE "guard|filter|guardrail|safety" services/ 2>/dev/null || fail "aucun filtre d'entrée/guardrail détecté"
pass "filtre d'entrée minimal présent"

# 5. make demo déroule un scénario de bout en bout
if ! make demo >&2 2>/dev/null; then
  fail "make demo échoue (scénario de démonstration bout-en-bout)"
fi
pass "make demo OK (scénario complet déroulé)"

echo "== verify-m6 OK (rapport GO/NO-GO, guardrail, demo) ==" >&2
