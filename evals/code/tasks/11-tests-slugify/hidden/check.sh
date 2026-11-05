set -eu
cd /work
cmp -s slug.py /check/orig_slug.py || { echo "slug.py was modified"; exit 1; }
test -f tests/test_slug.py || { echo "no tests/test_slug.py"; exit 1; }
n=$(grep -c "def test" tests/test_slug.py || true)
[ "$n" -ge 1 ] || { echo "no test methods"; exit 1; }
python3 -m unittest discover -s tests -q || { echo "tests fail on the real implementation"; exit 1; }
survivors=""
for m in /check/mutants/*.py; do
  rm -rf /tmp/mut && mkdir /tmp/mut && cp -r /work/. /tmp/mut/ && cp "$m" /tmp/mut/slug.py
  if (cd /tmp/mut && timeout 60 python3 -m unittest discover -s tests -q >/dev/null 2>&1); then survivors="$survivors $(basename $m .py)"; fi
done
rm -rf /tmp/mut
if [ -n "$survivors" ]; then echo "surviving mutants:$survivors"; exit 1; fi
echo "all mutants killed"
