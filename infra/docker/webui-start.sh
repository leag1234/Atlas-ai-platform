#!/bin/bash
set -euo pipefail
export PYTHONPATH=/opt/atlas:/app/backend
cd /app/backend
if [ "${ATLAS_REMOVE_OPEN_WEBUI_SUFFIX:-0}" = "1" ]; then
    sed -i "s/WEBUI_NAME += ' (Open WebUI)'/WEBUI_NAME = WEBUI_NAME/" /app/backend/open_webui/env.py
fi
python /opt/atlas/webui_bootstrap.py
exec bash /app/backend/start.sh
