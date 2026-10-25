"""Private evaluator entry point; never supplied to the evaluated worker."""

from evals.document_tasks.long_sessions import main

if __name__ == "__main__":
    raise SystemExit(main("D31"))
