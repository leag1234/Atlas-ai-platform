#!/usr/bin/env bash
set -Eeuo pipefail
# Basic secret detection; .env and secrets.env must be ignored.
if git ls-files 2>/dev/null | grep -qE '(^|/)(\.env|secrets\.env)$'; then
  echo "::error::a secrets file is tracked by git"; exit 1
fi
PATTERN='(AKIA|scw-[a-f0-9-]{36}|sk-ant-|ghp_|SG\.[A-Za-z0-9_-]{16,})'
if git grep -nIE "$PATTERN" -- . ':!*.example' ':!scripts/scan_secrets.sh' ':!services/orchestrator/static/vendor/*' 2>/dev/null; then
  echo "::error::potential secret detected in code"; exit 1
fi
echo "scan-secrets ok"
