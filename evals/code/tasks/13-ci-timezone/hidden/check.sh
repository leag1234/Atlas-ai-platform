set -eu
cd /work
cmp -s ci.sh /check/orig/ci.sh || { echo "ci.sh was modified"; exit 1; }
cmp -s tests/test_daily.py /check/orig/test_daily.py || { echo "tests were modified"; exit 1; }
[ "$(ls tests)" = "test_daily.py" ] || [ "$(ls tests | grep -v __pycache__)" = "test_daily.py" ] || { echo "unexpected files in tests/"; exit 1; }
for tz in NZST-12 UTC0 HST10 IST-5:30 XYZ+11 ABC-14; do
  CI_TZ=$tz sh ci.sh >/tmp/ci.out 2>&1 || { echo "ci.sh fails with TZ=$tz"; tail -20 /tmp/ci.out; exit 1; }
  TZ=$tz python3 -m unittest discover -s /check -p 'test_hidden.py' -q || { echo "hidden tests fail with TZ=$tz"; exit 1; }
done
