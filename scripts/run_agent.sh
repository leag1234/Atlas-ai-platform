#!/usr/bin/env bash
# Run ONE milestone with Codex CLI (OpenAI) in headless mode, then stop.
# The agent MERGES its own PR once CI is green (delegated merge).
# Codex builds the PoC; it is never a product runtime dependency.
# Usage : run_agent.sh M2
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
set -a; [[ -f .env ]] && source .env; set +a

MILESTONE="${1:?usage: run_agent.sh M<n>}"
mkdir -p BRAIN
echo "=== $(date -Is) — starting agent on $MILESTONE ===" | tee -a BRAIN/agent.log

read -r -d '' PROMPT <<EOF || true
Implement milestone ${MILESTONE} of the ATLAS-0 project.

Before taking any action: read AGENTS.md, MISSION.md, docs/13-poc-spec.md.
STRICTLY follow docs/11 and docs/14 (absolute rules, observability markers,
anti-rationalization, BRAIN/ state).

Contract (R-02): you MAY include the contract AND implementation in the SAME PR for
this milestone (authorized exception), unless the milestone explicitly requires a
separate contract. Aim for ONE PR per milestone when reasonable.

Definition of done for ${MILESTONE} :
  1. \`make verify-${MILESTONE,,}\` passes LOCALLY, then
  2. create branch ${MILESTONE,,}-<topic>, commit, push, open a Pull Request, then
  3. WAIT until the CI job 'ci' is GREEN: query
     GET https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/commits/<sha>/check-runs
     or /actions/runs, with the header "Authorization: Bearer \${GITHUB_TOKEN}", in a loop
     (max 60 attempts, 15s intervals = 15 min; repository CI can be slow), then
  4. IF AND ONLY IF CI is GREEN, MERGE the PR yourself using:
     curl -X PUT -H "Authorization: Bearer \${GITHUB_TOKEN}" -H "Accept: application/vnd.github+json" \\
       https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/pulls/<N>/merge \\
       -d '{"merge_method":"squash"}'
     then update BRAIN/ and STOP. DO NOT move to the next milestone.

MERGE AUTHORIZATION: you may merge your own PRs, on the STRICT CONDITION
that the 'ci' job is green. NEVER merge a PR with failed or pending CI. NEVER modify
a protected file (CODEOWNERS, scripts/verify-*, .github/workflows).
If blocked after 3 attempts on the same issue: write to BRAIN/BLOCKERS.md and
stop cleanly. Never claim a test passes without CI evidence.
EOF

SANDBOX="${CODEX_SANDBOX:-danger-full-access}"
set +e
codex exec "$PROMPT" \
  --dangerously-bypass-approvals-and-sandbox \
  --json \
  --output-last-message "BRAIN/${MILESTONE}.last.txt" \
  >> "BRAIN/${MILESTONE}.jsonl" 2>> BRAIN/agent.log
CODE=$?
set -e

echo "=== $(date -Is) — agent on $MILESTONE finished (exit $CODE) ===" | tee -a BRAIN/agent.log
exit $CODE
