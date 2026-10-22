#!/usr/bin/env bash
# verify-m7 — UI de test + observabilité. PROTÉGÉ (CODEOWNERS).
#
# Vérifie que le système est interrogeable de bout en bout via une API
# OpenAI-compatible, que le logging structuré des interactions fonctionne,
# et qu'Open WebUI est joignable. Sans GPU par défaut (escalade Scaleway).
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m7: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m7 ==" >&2

# 1. l'adaptateur OpenAI-compatible existe
grep -rqiE "chat/completions|chat_completions|openai" services/ 2>/dev/null || fail "adaptateur OpenAI-compatible absent"
pass "adaptateur OpenAI-compatible présent"

# 2. make serve existe
grep -qE "^serve:" Makefile || fail "cible 'make serve' absente"
pass "cible make serve présente"

# 3. démarrer les services et tester une requête chat de bout en bout
#    (l'agent fournit un test d'intégration qui lève les services, envoie une
#     requête, vérifie la réponse + la production d'un log structuré)
if ! make test-ui >&2 2>/dev/null; then
  fail "test d'intégration UI échoue (requête /v1/chat/completions de bout en bout)"
fi
pass "requête /v1/chat/completions répond de bout en bout"

# 4. logging structuré : une ligne d'interaction avec les champs requis
LOGDIR="BRAIN/interactions"
LATEST=$(ls -t "$LOGDIR"/*.jsonl 2>/dev/null | head -1 || true)
[[ -n "$LATEST" ]] || fail "aucun log d'interaction produit dans $LOGDIR"
# vérifier les champs minimaux sur la dernière ligne
python3 - "$LATEST" << 'PYEOF'
import sys, json
last = None
for line in open(sys.argv[1]):
    line=line.strip()
    if line: last=line
if not last:
    print("::error::verify-m7: log d'interaction vide"); sys.exit(1)
d = json.loads(last)
required = ["question","reponse","modele_utilise","latence_ms","citations","tokens","cout_eur"]
missing = [k for k in required if k not in d]
if missing:
    print(f"::error::verify-m7: champs manquants dans le log: {missing}"); sys.exit(1)
print("  ✓ log structuré complet:", list(d.keys()), file=sys.stderr)
PYEOF

# 5. Open WebUI joignable sur le port 3000
code=$(curl -s -o /dev/null -w '%{http_code}' -m 8 "http://127.0.0.1:3000" 2>/dev/null || echo 000)
[[ "$code" =~ ^(200|302|401)$ ]] || fail "Open WebUI non joignable sur :3000 (code $code)"
pass "Open WebUI joignable sur :3000 (code $code)"

echo "== verify-m7 OK (UI interrogeable, logging structuré, Open WebUI up) ==" >&2
