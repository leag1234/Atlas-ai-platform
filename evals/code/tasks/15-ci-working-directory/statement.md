The CI job `sh ci.sh` fails (log in `ci-log.txt`) although `python3 -m unittest discover -s tests` passes from
the repository root. Find the root cause and fix the `orders` package so that CI passes and the package works
from any current working directory and when installed elsewhere. Do not modify `ci.sh`, `tests/` or the
schema file.
