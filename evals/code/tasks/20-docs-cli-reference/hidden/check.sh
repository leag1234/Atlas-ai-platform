set -eu
cd /work
python3 -m unittest discover -s tests -q
python3 /check/check_docs.py
# the parser must be unchanged (help texts may be added)
python3 /check/parser_sig.py /work > /tmp/new.json
python3 /check/parser_sig.py /check/orig > /tmp/old.json
cmp -s /tmp/new.json /tmp/old.json || { echo "argument parser changed"; exit 1; }
