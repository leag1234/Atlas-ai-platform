# M18 conversation integrity and image fidelity

Source: MISSION M18 D1–D6, contracts/m18.md, REQ-ENG-004/009 and REQ-FIN-002.

Follow-up transformations now run against the previous answer with tools disabled,
after project scope resolution. Streaming preserves existing source links. Generated
images are served by opaque references; legacy inline image bytes are removed before
context validation. Image iterations retain the original request and requested changes.
Explicit image-edit requests receive the localized unsupported-operation explanation.

Transient search failures get an automatic query reformulation within the existing
search/tool/time/cost ledger. Status labels use fixed resources selected by `ui_locale`,
independently of answer language. Image prompts are rewritten into structured English;
original and rewritten prompts, reproducible random seeds and image references are logged.
The worker uses 1024px, a checked 512-token limit, guidance zero and a 180-second watchdog.
The combined rewrite/generation request remains capped at EUR0.05 and 120 seconds.

## Evidence

- [Live public journeys](journeys-live.json): J1–J7 and J9–J14 completed using real
  external exchanges. Completed exchanges were reused within the session during
  recovery, as indicated in the report. J12 injects its first timeout in tests only.
  Two real search retries timed out; the third bounded run succeeded. No provider
  availability claim is inferred from the injected faults, and no fourth attempt ran.
- [J14 prompt, seed and vision assessment](j14.json) and [image](j14.png): **3/3**
  constraints observed by the vision provider. This single result is not a general
  guarantee of image fidelity.
- [Matched-seed four/eight-step comparison](measurement.md): four steps retained
  for equal observed constraint fidelity and lower latency.
- Local protected `make verify-m18` completed with exit 0 in replay mode, replaying
  the actual recorded provider exchanges through the public HTTP API. Its separate
  live-stack probe was skipped because the test stack is created inside the journey
  harness. Live J9 separately exercised the same follow-up behavior.
- Baseline commit `8155715` fails the three new follow-up/history/retry regressions.
  Local lint, strict type checking and secret scanning completed with exit 0.

The owner confirmed credential rotation in MISSION.md; that incident is closed.
J8 completed both successive starts and orderly cleanup with exit 0. GitHub CI
is pending. The first CI run exposed the M7 cassette's outdated system prompt.
M7 was recorded again against the real provider (EUR0.0115213), preserving exact
request matching and all assertions; its local replay completed with exit 0.
Local final checks ran
322 tests, lint, strict type checking and secret scanning successfully. These
local results do not establish milestone completion without green GitHub CI.

## Limits and costs

No dependencies, protected verifiers or workflows were changed. Project history is
rebuilt from the server before routing; developer API authentication/quotas and MCP
confirmation remain covered by their existing gates. The image model/licence choice
is documented in [the runbook](../../runbooks/m18-images.md).

The experimental GPU was billed at EUR1.46988/hour. Cleanup removed the owned GPU,
root disk and IP; a fresh dedicated-project inventory shows only the preexisting CPU
VM. The reusable model-weight volume remains as designed. No GPU is left running.
