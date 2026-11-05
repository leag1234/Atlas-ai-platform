set -eu
cd /work
cmp -s ci.sh /check/orig/ci.sh || { echo "ci.sh was modified"; exit 1; }
for f in test_aliases.py test_tags.py; do cmp -s tests/$f /check/orig/$f || { echo "tests/$f was modified"; exit 1; }; done
sh ci.sh
python3 -m unittest -q tests.test_tags tests.test_aliases
python3 /check/shuffle.py
python3 -m unittest discover -s /check -p 'test_hidden.py' -q
