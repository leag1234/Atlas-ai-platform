set -eu
cd /work
cmp -s tests/test_ledger.py /check/orig_test_ledger.py || { echo "existing tests were modified"; exit 1; }
python3 -m unittest discover -s tests -q
python3 -m unittest discover -s /check -p 'test_hidden.py' -q
