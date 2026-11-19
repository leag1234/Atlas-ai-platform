"""Long-form answer prototype (not wired into the product).

Run one request: python run.py <case> <request-file>, from a directory that also holds
vendor/mermaid.min.js (mermaid@11.4.1 dist, used to validate and render diagrams), with
PYTHONPATH covering this directory and the repository root (packages.language,
services.orchestrator.kiwix). runcase.sh shows the setup used on the build VM.

Flow: genre and depth (request.py), expert-perspective and source survey (evidence.survey),
outline checked against the request's expectations and the genre skeleton (outline.py),
evidence per section with quotes verified on the page (evidence.gather), derived figures
evaluated by Python (derive.py), section writing (write.py), checks and one targeted repair
(checks.py, pipeline.py), Markdown, HTML and PDF output with a JSON trace (render.py).
Measurement rubric: RUBRIC.md.
"""

import sys
import time
from pathlib import Path

from longform.pipeline import run

ROOT = Path(__file__).resolve().parent

if __name__ == "__main__":
    case, request_file = sys.argv[1], sys.argv[2]
    text = Path(request_file).read_text().strip()
    log_file = (ROOT / "out" / case)
    log_file.mkdir(parents=True, exist_ok=True)
    handle = (log_file / "run.log").open("w")

    def log(message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        print(line, flush=True)
        handle.write(line + "\n")
        handle.flush()

    run(case, text, ROOT / "out", log)
