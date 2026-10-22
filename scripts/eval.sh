#!/usr/bin/env bash
set -Eeuo pipefail
python3 evals/harness.py "${@:---smoke}"
