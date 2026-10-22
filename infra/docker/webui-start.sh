#!/bin/bash
set -euo pipefail
PYTHONPATH=/app/backend python /opt/atlas/webui_bootstrap.py &
exec bash /app/backend/start.sh
