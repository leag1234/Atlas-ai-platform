The CI job `sh ci.sh` fails on the CI runner, while developers say the tests pass on their laptops. The log of
the last failing CI run is in `ci-log.txt`.

Find the root cause and fix the application code so that `sh ci.sh` passes, whatever time zone the machine
uses. All dates in this project are UTC by specification (see `metrics/daily.py`). Do not modify `ci.sh` or
anything under `tests/`.
