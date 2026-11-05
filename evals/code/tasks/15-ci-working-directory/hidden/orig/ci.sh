#!/bin/sh
# CI entry point: the runner builds out of tree, in ./build.
set -eu
ROOT=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$ROOT/build"
cd "$ROOT/build"
PYTHONPATH="$ROOT" python3 -m unittest discover -s "$ROOT/tests" -t "$ROOT" -v
PYTHONPATH="$ROOT" python3 -m orders.validate "$ROOT/examples/good.json"
