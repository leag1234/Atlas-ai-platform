set -eu
cd /work
test -f report.py || { echo "report.py missing"; exit 1; }
for name in sample hidden1 hidden2; do
  case $name in sample) input=/check/sample.csv;; *) input=/check/$name.csv;; esac
  python3 report.py "$input" > /tmp/out.md || { echo "report.py failed on $name"; exit 1; }
  cmp -s /tmp/out.md /check/$name.expected.md || { echo "output differs on $name"; diff /check/$name.expected.md /tmp/out.md | head -20; exit 1; }
done
(cd /tmp && python3 /work/report.py /tmp/does-not-exist.csv >/dev/null 2>/tmp/err) && { echo "missing file must exit 1"; exit 1; }
test -s /tmp/err || { echo "missing file must print an error"; exit 1; }
