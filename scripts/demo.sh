#!/usr/bin/env bash
set -Eeuo pipefail
export PYTHONPATH=".:tests:services/model-gateway${PYTHONPATH:+:$PYTHONPATH}"
python3 tests/m6_demo.py
