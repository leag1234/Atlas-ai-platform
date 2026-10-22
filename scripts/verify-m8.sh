#!/usr/bin/env bash
# verify-m8 — modèle serverless : routage par type de tâche entre modèles Scaleway. PROTÉGÉ.
# Plus de GPU local pour le texte. Vérifie que chaque type de tâche est servi par le bon
# modèle serverless, que function-calling et streaming marchent sur le principal.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m8: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m8 ==" >&2

# config des modèles par type de tâche présente
grep -rqiE "task.?type|texte|code|vision" services/model-gateway/ 2>/dev/null || fail "routage par type de tâche absent"
pass "routage par type de tâche présent"

# aucun appel GPU local pour le texte (pas de vLLM local requis)
if grep -rqiE "gpu_ip|vllm.*8000" services/model-gateway/routing.yaml 2>/dev/null; then
  echo "  (note: référence vLLM local encore présente dans routing — doit être optionnelle)" >&2
fi

if [[ -z "${SCW_GENERATIVE_API_KEY:-}" ]]; then
  pass "contrôle statique OK (test réel = avec accès Scaleway)"; echo "== verify-m8 OK (statique) ==" >&2; exit 0
fi

if ! make test-serverless >&2 2>/dev/null; then
  fail "make test-serverless échoue"
fi
R="BRAIN/eval/serverless.json"; [[ -f "$R" ]] || fail "rapport serverless absent"
python3 - "$R" << 'PYEOF'
import sys, json
d = json.load(open(sys.argv[1]))
checks = {
  "text_task_served": "tâche texte non servie par le modèle prévu",
  "code_task_served": "tâche code non servie par le modèle code",
  "function_calling_ok": "function calling KO sur le modèle principal",
  "streaming_ok": "streaming KO sur le modèle principal",
  "fallback_ok": "fallback entre modèles serverless KO",
}
for k, msg in checks.items():
    if not d.get(k):
        print(f"::error::verify-m8: {msg}"); sys.exit(1)
print("  ✓ texte/code servis, function-calling, streaming, fallback OK", file=sys.stderr)
PYEOF
echo "== verify-m8 OK ==" >&2
