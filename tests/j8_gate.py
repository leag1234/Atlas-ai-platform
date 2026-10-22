"""Run the real startup journey once for unchanged production inputs per workspace."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys


def startup_journey() -> dict[str, object]:
    inputs = sorted(
        p
        for root in ("services", "packages")
        for p in Path(root).rglob("*")
        if p.suffix in {".py", ".json", ".yaml", ".env"}
    )
    inputs += [
        Path(p)
        for p in (
            "Makefile",
            "scripts/serve.sh",
            "infra/chat-ui.env",
            "requirements-dev.txt",
            "tests/serve_idempotent.py",
        )
    ]
    digest = hashlib.sha256()
    for path in inputs:
        digest.update(path.as_posix().encode())
        digest.update(path.read_bytes())
    fingerprint = digest.hexdigest()
    report_path = Path("BRAIN/eval/serve-idempotent.json")
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    if (
        report.get("fingerprint") != fingerprint
        or report.get("J8_second_serve_ready") is not True
    ):
        subprocess.run([sys.executable, "tests/serve_idempotent.py"], check=True)
        report = json.loads(report_path.read_text())
        assert report.get("J8_second_serve_ready") is True
        report["fingerprint"] = fingerprint
        report_path.write_text(json.dumps(report, indent=2) + "\n")
    return {"J8_second_serve_ready": True, "j8_production_fingerprint": fingerprint}
