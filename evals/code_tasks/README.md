# Code task set

This directory contains the internal, offline evaluation set for Code mode. Each task directory has a public `task.json` and a compressed `base.tar.gz` snapshot. Its verifier and known-good oracle solution live in `verifiers/` and are copied into the temporary workspace only after an agent has finished.

To add a task, add a deterministic fixture to `build_fixtures.py`, run that builder, and commit its task directory plus matching verifier directory. A task must use only the standard library, have a snapshot below 200 KB, use a plain-English issue prompt that does not name the fix, and have a stable verifier command using the virtualenv Python.

The harness accepts a Python callable `(prompt, workspace) -> final_claim`; `command_agent()` adapts a command. `null_agent` and `oracle_agent` are reference agents. Run `python evals/code_tasks/run.py --agent oracle --report /tmp/oracle.json` to write a per-task JSON report. Score is solved verifier exits divided by all tasks. A final claim containing a success word while the verifier fails is counted as a false success claim.

The task set is measurement-only: never use it to tune prompts, agent instructions, or model behavior. Keep verifier files hidden from evaluated agents.
