# Plan M1 — minimal GPU cycle and gateway

Status: ready for review before implementation (docs/11 §9.2).
Source: MISSION.md at commit aebb3e1, contracts/m1.md, docs/13 POC-A1/A3/A4,
POC-I1/I2 ; docs/03 REQ-INF-012/013, docs/04 REQ-FIN-002,
docs/11 REQ-ENG-004/005/009/011.

## Contract and scope
A real cycle on the VM: creation, vLLM readiness, inference, JSON bench,
destruction, total duration < 20 minutes. Then PR and green GitHub `ci` job.
CI without GPU: protected static analysis and lifecycle tests with mocks
strictly limited to tests. No TTFT SSE nor second cycle in M1.
The minimal contract is already in the local history; verify the remote merge
before implementation. No migration, new UI, RAG, or M2 realization.

## Increments and files
1. Tests `tests/test_m1.py`: missing/invalid configuration, project isolation,
   provider errors, idempotence, ambiguous double instance, cleanup on failure,
   preservation of weights volume, deletion of system disk and IP.
   Show their failure on current scripts before the fix.
2. `infra/gpu-up.sh`, `infra/gpu-down.sh`, cloud-init resources under `infra/`:
   Scaleway CLI with explicit project/zone, selection by ID and ownership tags,
   local lock against concurrent cycles and atomic state without secrets.
   Create/reattach a persistent Block Storage volume for weights; never
   reformat an existing volume. Bootstrap via cloud-init, without SSH installation.
   Pin the GPU image and container after compatibility verification.
   Enable prefix caching, serve the `local` alias, mount the weights cache on
   the persistent volume. Bound waits and propagate errors.
   Clean up all owned temporary resources, including partial failures;
   retain only the weights volume agreed upon in the contract.
3. `services/model-gateway/`: local configuration and Scaleway fallback,
   model identifiers exclusively in this service or the environment.
   Local URL from provisioning; alias/URL consistency tests.
   Document usage, cost, errors, and recovery in the README.
4. `infra/inventory.sh`: inventory explicitly bounded to the dedicated project,
   usable before/after the cycle and at session closure.
5. `scripts/test.sh`: include M1 static analysis in the existing CI path,
   without calling the GPU and without modifying protected files.
   Adapt quality targets if Python is added under infra.
6. BRAIN: state before provisioning and push, real bench, PR/CI references,
   final inventory. No secrets or raw traces added to Git.

## Verifications and delivery
- Tests without network: invalid inputs, errors and timeouts, untouched foreign
  resources, visible cleanup failure, preserved weights, cloud-init and routing.
- Shell syntax, lint, typing, tests, secret scan; diff coverage ≥ 80 %.
- Real `make verify-m1` on the VM after cloud prerequisites; also inspect the JSON
  and final inventory without weakening the protected verifier assertions.
- Maximum three attempts per issue; log then stop on the third.
- Push only to branch `m1-*`, PR < 400 lines of diff; if necessary,
  split into coherent PRs and wait for human merges before dependencies.
- Wait for the green `ci` job on the delivered commit, cite its URL; update
  JOURNAL/STATUS/TASK and verify the CI of the last published commit. No agent merge.
- No file .github/workflows/, scripts/verify-*.sh or CODEOWNERS modified.

## Prerequisites and risks to resolve before launch
- Confirmation of independent alerts at 50/80 % of €800 and of planned shutdown
  (docs/13 POC-I1/I2, docs/14 AUTO-2/3).
- GPU configured: L40S-1-48G, fr-par-2, 1.469916 EUR/h excl. tax in the API catalog
  consulted on 2026-09-09. Announced availability: shortage.
- Estimate costs for the weights volume, system disk, and IP separately before creation,
  record the total and persistent cost in BRAIN/STATUS.md.
- Protect the inference port with a network rule limited to the verification VM.
- Verify real CLI signatures, Block Storage mounting, GPU/vLLM versions
  and weights revision; no invented API or compatibility.
- Tests do not prove commercial availability or first download speed:
  only the real cycle satisfies the GPU proof.
