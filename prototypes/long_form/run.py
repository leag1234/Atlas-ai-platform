"""Run the long-form prototype on one request: python run.py <case> <request-file>."""

import os
import sys
import time
from pathlib import Path

from longform.pipeline import run

ROOT = Path(__file__).resolve().parent

if __name__ == "__main__":
    case, request_file = sys.argv[1], sys.argv[2]
    text = Path(request_file).read_text().strip()
    out_root = ROOT / os.environ.get("LONGFORM_OUT", "out")
    log_file = out_root / case
    log_file.mkdir(parents=True, exist_ok=True)
    handle = (log_file / "run.log").open("w")

    def log(message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        print(line, flush=True)
        handle.write(line + "\n")
        handle.flush()

    run(case, text, out_root, log)
