set -eu
cd /work
diff -r vendor_dist /check/orig/vendor_dist >/dev/null || { echo "vendor_dist was modified"; exit 1; }
cmp -s SECURITY-ADVISORY.md /check/orig/SECURITY-ADVISORY.md || { echo "advisory was modified"; exit 1; }
diff -r -x __pycache__ vendor/packkit /check/orig/vendor_dist/packkit-2.0.0/packkit || { echo "vendor/packkit is not an exact copy of 2.0.0"; exit 1; }
python3 -m unittest discover -s tests -q
python3 -m unittest discover -s /check -p 'test_hidden.py' -q
