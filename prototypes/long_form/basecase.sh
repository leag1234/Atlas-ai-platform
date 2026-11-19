#!/bin/bash
cd /opt/atlas-src/proto-longform
export LONGFORM_KEY_FILE=/root/atlas-keys/staging/proto-longform.key
export ATLAS_KIWIX_URL="$(grep -E '^ATLAS_KIWIX_URL=' /opt/atlas-src/staging.env | cut -d= -f2-)"
export ATLAS_KIWIX_BOOKS="$(grep -E '^ATLAS_KIWIX_BOOKS=' /opt/atlas-src/staging.env | cut -d= -f2-)"
PYTHONPATH=/opt/atlas-src/proto-longform:/opt/atlas-src/wt-longform exec /opt/atlas-src/repo/.venv/bin/python baseline.py "$1" "cases/$1.txt"
