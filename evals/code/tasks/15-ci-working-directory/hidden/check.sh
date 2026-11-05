set -eu
cd /work
cmp -s ci.sh /check/orig/ci.sh || { echo "ci.sh was modified"; exit 1; }
cmp -s tests/test_validate.py /check/orig/test_validate.py || { echo "tests were modified"; exit 1; }
cmp -s orders/schemas/order.json /check/orig/order.json || { echo "schema was modified"; exit 1; }
sh ci.sh
rm -rf build
for d in / /tmp /work/orders; do
  (cd $d && PYTHONPATH=/work python3 -m orders.validate /work/examples/good.json) || { echo "fails from $d"; exit 1; }
done
# installed elsewhere: copy the package to another directory and run it from there
rm -rf /tmp/site && mkdir /tmp/site && cp -r /work/orders /tmp/site/
(cd / && PYTHONPATH=/tmp/site python3 -c "import orders.validate as v, sys; sys.exit(0 if v.__file__.startswith('/tmp/site') and v.errors({'id':'1','customer':'c','lines':[]}) == [] else 1)")
# the schema must be read from the package: changing the packaged schema must change behaviour
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("/tmp/site/orders/schemas/order.json")
s = json.loads(p.read_text()); s["required"]["channel"] = "string"; p.write_text(json.dumps(s))
PY
(cd / && PYTHONPATH=/tmp/site python3 -c "import orders.validate as v, sys; sys.exit(0 if v.errors({'id':'1','customer':'c','lines':[]}) == ['missing channel'] else 1)") || { echo "schema not loaded from the package"; exit 1; }
rm -rf /tmp/site
