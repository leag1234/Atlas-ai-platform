"""Own the temporary HTTP server for the protected M0 verifier."""

import os
import subprocess

from server import running_server

with running_server() as url:
    result = subprocess.run(
        ["bash", "scripts/verify-m0.sh"],
        env={**os.environ, "BFF_URL": url, "ATLAS_VERIFY_M0": "1"},
    )
raise SystemExit(result.returncode)
