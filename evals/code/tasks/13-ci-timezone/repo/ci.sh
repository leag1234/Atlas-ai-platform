#!/bin/sh
# CI entry point: runs exactly what the CI runner runs. The runner is configured with the time zone below.
set -eu
export TZ="${CI_TZ:-NZST-12}"
python3 -m unittest discover -s tests -v
