"""Runs only inside a second keyless, networkless container with read-only input."""

import hashlib
import json
from pathlib import Path
import runpy

path = Path("/work/clean_text.py")
function = runpy.run_path(str(path))["normalize_whitespace"]
cases = [
    ("", ""),
    ("  a\tb\n ", "a b"),
    ("a\u00a0\u2003b", "a b"),
    (" déjà  vu ", "déjà vu"),
]
assert all(function(value) == expected for value, expected in cases)
print(
    json.dumps(
        {"checks": len(cases), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    )
)
