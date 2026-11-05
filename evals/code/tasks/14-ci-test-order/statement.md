`sh ci.sh` fails in CI, but every test module passes when a developer runs it on its own
(`python3 -m unittest tests.test_tags`, `python3 -m unittest tests.test_aliases`). The CI log is in `ci-log.txt`.

Find the root cause and fix it in the application code (`tagger/`), so that the tests pass in any order and
any combination. Do not modify `ci.sh` or the tests. The docstrings describe the intended behaviour.
