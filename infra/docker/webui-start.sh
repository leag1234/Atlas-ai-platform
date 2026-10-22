#!/bin/bash
set -euo pipefail
export PYTHONPATH=/opt/atlas:/app/backend
cd /app/backend
python /opt/atlas/webui_bootstrap.py
exec bash /app/backend/start.sh
