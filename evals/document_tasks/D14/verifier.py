"""Private verifier entry point; never supplied to the agent under test."""

from evals.document_tasks.verifier import main

if __name__ == "__main__":
    raise SystemExit(main("D14"))
