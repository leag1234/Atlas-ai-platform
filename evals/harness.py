"""POC-R1/R4: dispatch the full evaluation or the recorded PR smoke."""

import argparse
import os
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--smoke", action="store_true")
    mode.add_argument("--full", action="store_true")
    args = parser.parse_args()
    script = "tests/eval_smoke.py" if args.smoke else "tests/m5_gate.py"
    env = dict(os.environ)
    env["PYTHONPATH"] = ".:tests:services/model-gateway:services/edge-bff"
    result = subprocess.run(
        [
            "timeout",
            "--signal=INT",
            "--kill-after=30",
            "180" if args.smoke else "1200",
            "python3",
            script,
        ],
        env=env,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
