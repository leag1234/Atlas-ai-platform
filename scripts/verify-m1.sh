#!/usr/bin/env bash
# verify-m1 — infra GPU : le nœud naît, sert un modèle, meurt. PROTÉGÉ (CODEOWNERS).
#
# Preuve SUR LA VM (GPU + creds), PAS en CI (ni GPU ni secrets).
# But minimal du PoC : prouver qu'un GPU se crée par script, que vLLM répond,
# et que le GPU se détruit. Un trap détruit le GPU en sortie quoi qu'il arrive.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m1: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m1 ==" >&2

# --- CI ou VM sans creds : contrôle statique seulement ---
if [[ -z "${SCW_ACCESS_KEY:-}" || -z "${SCW_SECRET_KEY:-}" ]]; then
  echo "  (creds absents : contrôle statique — normal en CI)" >&2
  bash -n infra/gpu-up.sh   || fail "gpu-up.sh : syntaxe"
  bash -n infra/gpu-down.sh || fail "gpu-down.sh : syntaxe"
  [[ -x infra/gpu-up.sh && -x infra/gpu-down.sh ]] || fail "gpu-*.sh non exécutables"
  grep -q "enable-prefix-caching" infra/gpu-up.sh || fail "prefix caching absent (docs/04)"
  pass "contrôle statique OK"
  echo "== verify-m1 OK (statique) ==" >&2
  exit 0
fi

# --- VM avec creds : test réel, un cycle ---
mkdir -p BRAIN/bench
cleanup(){ echo "  [cleanup] destruction GPU…" >&2; bash infra/gpu-down.sh >&2 || true; }
trap cleanup EXIT

start=$(date +%s)
echo "  → création du nœud GPU…" >&2
bash infra/gpu-up.sh >&2 || fail "gpu-up.sh a échoué"
ip="$(cat BRAIN/gpu_ip.txt 2>/dev/null || true)"
[[ -n "$ip" ]] || fail "pas d'IP produite"
pass "nœud créé (IP $ip)"

echo "  → attente de vLLM (max 12 min)…" >&2
ready=0
for _ in $(seq 1 72); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://$ip:8000/v1/models" 2>/dev/null || echo 000)
  [[ "$code" == "200" ]] && { ready=1; break; }
  sleep 10
done
[[ "$ready" == "1" ]] || fail "vLLM n'a pas répondu 200 en 12 min"
pass "vLLM répond sur /v1/models"

echo "  → inférence de contrôle…" >&2
t0=$(date +%s.%N)
resp=$(curl -s -m 60 "http://$ip:8000/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"local","max_tokens":64,"messages":[{"role":"user","content":"Dis bonjour en une phrase."}]}' \
  2>/dev/null || true)
t1=$(date +%s.%N)
echo "$resp" | grep -q '"choices"' || fail "pas de complétion valide"
dur=$(awk "BEGIN{printf \"%.2f\", $t1 - $t0}")
tok=$(echo "$resp" | grep -o '"completion_tokens":[0-9]*' | grep -o '[0-9]*' || echo 0)
tps=$(awk "BEGIN{d=$dur; printf \"%.1f\", (d>0 ? ${tok:-0}/d : 0)}")

bench="BRAIN/bench/m1-$(date +%Y%m%dT%H%M%SZ).json"
printf '{"ip":"%s","total_s":%s,"output_tokens":%s,"tokens_per_s":%s,"ts":"%s"}\n' \
  "$ip" "$dur" "${tok:-0}" "$tps" "$(date -Is)" > "$bench"
pass "inférence OK — ${tok} tokens en ${dur}s (~${tps} tok/s), bench: $bench"

echo "  → destruction du nœud…" >&2
bash infra/gpu-down.sh >&2 || fail "gpu-down.sh a échoué"
trap - EXIT

elapsed=$(( $(date +%s) - start ))
[[ "$elapsed" -lt 1200 ]] || fail "cycle trop lent : ${elapsed}s (< 1200s attendu)"
pass "cycle complet en ${elapsed}s (< 20 min)"
echo "== verify-m1 OK (GPU créé, modèle servi, GPU détruit) ==" >&2
