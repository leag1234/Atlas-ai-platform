set -eu
cd /work
test -f daily_active.py || { echo "daily_active.py missing"; exit 1; }
for name in sample hidden1 hidden2; do
  python3 daily_active.py /check/$name.csv > /tmp/out.json || { echo "daily_active.py failed on $name"; exit 1; }
  cmp -s /tmp/out.json /check/$name.expected.json || { echo "output differs on $name"; diff /check/$name.expected.json /tmp/out.json | head -20; exit 1; }
done
