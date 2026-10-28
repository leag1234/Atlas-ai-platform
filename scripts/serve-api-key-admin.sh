#!/usr/bin/env bash
# REQ-KEY-02: the administration service is never attached to a public proxy.
set -Eeuo pipefail
: "${ATLAS_API_KEY_PEPPER:?Set the private API key pepper in the environment}"
: "${ATLAS_API_KEY_ADMIN_TOKEN:?Set the private administrator token in the environment}"
exec python3 -m uvicorn services.orchestrator.api_key_admin:app \
  --host 127.0.0.1 --port "${ATLAS_API_KEY_ADMIN_PORT:-8031}" --no-access-log
