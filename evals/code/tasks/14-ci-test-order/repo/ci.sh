#!/bin/sh
# CI entry point: the runner executes the suite as one process, in this order.
set -eu
python3 -m unittest -v tests.test_aliases tests.test_tags
