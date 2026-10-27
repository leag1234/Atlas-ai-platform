"""Runner-only process startup; cleanup continues independently of Work turns."""

import threading
import logging

import uvicorn

from services.runner.api import broker
from services.runner.network import initialize


def sweep(stop: threading.Event) -> None:
    while not stop.wait(60):
        try:
            broker().sweep()
        except (OSError, ValueError):
            logging.getLogger(__name__).warning(
                "runner_cleanup_requires_reconciliation"
            )


def main() -> None:
    initialize()
    stop = threading.Event()
    worker = threading.Thread(target=sweep, args=(stop,), daemon=True)
    worker.start()
    try:
        uvicorn.run(
            "services.runner.api:app", host="0.0.0.0", port=13712, access_log=False
        )
    finally:
        stop.set()
        worker.join(timeout=10)


if __name__ == "__main__":
    main()
