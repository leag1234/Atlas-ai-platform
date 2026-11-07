# Artefact continuation benchmark

This benchmark measures the complete small-app path: creation, scripted use, a
change request, a new-version check, and export. It is designed for the
supervisor to run against a deployed stack; it makes no live call in unit
tests.

## Run

```bash
python3 evals/artifacts/run.py \
  --base-url https://atlas.example.test \
  --api-key "$ATLAS_API_KEY" \
  --output evals/artifacts/results/run.json \
  --screenshot-dir /tmp/atlas-artifact-screenshots

python3 evals/artifacts/report.py evals/artifacts/results/run.json \
  > evals/artifacts/results/summary.md
```

The default profiles come from `contracts/m21-profiles.json`. Pass
`--profile` once per profile to select a subset, and `--repetitions` to change
the run count. The API key is accepted from the CLI or `ATLAS_API_KEY` and is
never written to results.

The runner sends non-streaming OpenAI-compatible chat requests, extracts the
public open and download links, opens the artefact in Chromium, performs the
case-specific interaction, verifies the new version, byte-compares the
download with the stored route, reopens the file from disk, and captures
light, dark, and phone screenshots.

Raw results retain every step, answer, usage, timing, and screenshot path.
The report aggregates step success, full-scenario success, first-preview
percentiles, change turnaround, language and operability signals, console
errors, cost, and token counts.
