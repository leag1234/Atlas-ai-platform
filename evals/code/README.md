# Code benchmark (phase 0)

Twenty small, self-contained coding tasks with hidden automatic checks. The benchmark compares coding agents
on identical work and separates the model gap from the harness gap:

| Harness \ Model | Reference model | Atlas models |
|---|---|---|
| Codex CLI or Claude Code | A: reference | B: model gap (`codex-atlas`, `codex-atlas-high`) |
| Atlas Work mode | n/a | C: harness gap, C against B (`atlas-work`) |

All task repositories are synthetic, use fictional names and need only Python 3.12 and its standard library.
Every check runs offline, in a container started with `--network none`.

## Layout

```
tasks/<id>/task.json      id, category, title, time budget (seconds)
tasks/<id>/statement.md   what the agent is asked (English)
tasks/<id>/repo/          repository snapshot given to the agent
tasks/<id>/hidden/        check.sh and its fixtures; never shown to the agent
tasks/<id>/solution/      reference solution overlay (plus optional .delete list), used to validate checks
image/Dockerfile          image used for checks and for the Codex configurations
run.py                    runner
atlas_work.py             adapter for configuration C (guest Work mode)
results/                  dated results (JSON) and summaries (Markdown)
```

Categories: bug fix with hidden test (4), small feature (3), multi-file refactor (3), writing tests (2, judged by
mutants), diagnosing a failing CI script (3), CSV to report data script (2), dependency update with known
CVEs (2, vendored offline metadata), documentation (1).

## Running

```
docker build -t atlas-code-bench:local evals/code/image
python3 evals/code/run.py --config oracle            # must pass 20/20
python3 evals/code/run.py --config null              # must pass 0/20
python3 evals/code/run.py --config codex-atlas --out evals/code/results/<date>-codex-atlas.json
python3 evals/code/report.py evals/code/results/<date>-*.json > evals/code/results/<date>-summary.md
```

For each task the runner copies `repo/` into a throwaway git working copy, gives the agent a fixed preamble
plus `statement.md`, then copies the result (without `.git`) into a fresh directory and runs
`hidden/check.sh` there with the hidden directory mounted read-only at `/check`. Recorded per task: pass/fail,
wall time, time budget, steps (commands, file changes, other tool calls), tokens, cost, the agent's final
claim and whether it was a false success claim. Agent logs and the diff go to `BENCH_LOG_DIR`
(default `/opt/atlas-src/bench-logs`, outside the repository).

The preamble asks the agent to end with `RESULT: SUCCESS` or `RESULT: FAILURE`. A false claim is a claimed
(or, without that line, implied) success while the hidden check fails.

Cost: `charged_eur` is the Atlas API key usage delta measured before and after the task (configuration B)
or the Work session usage (configuration C). `cost_estimate_eur` recomputes B from the token counts and the
provider prices of the first route of the profile.

Configuration B runs Codex CLI inside the benchmark image with only the working copy mounted, so the agent
cannot read hidden checks. Configuration C runs the Work mode of the guest instance; that Work session has
no export route, so the adapter rebuilds the workspace from the diffs in the Work event stream.

The task set is measurement only: do not tune prompts or agent instructions on it.
