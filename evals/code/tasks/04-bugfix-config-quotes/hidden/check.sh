set -eu
cd /work
python3 -m unittest discover -s tests -q
python3 -m unittest discover -s /check -p 'test_hidden.py' -q
