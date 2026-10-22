# model-gateway

Owner: `OWNERS`.

M1 (REQ-INF-012/013): provisioning engine `python3 infra/gpu.py up/down`.
Parameters: Scaleway project/zone, LOCAL_MODEL, GPU_CLIENT_IP, GPU_MAX_EUR_H.
SLO, runbook, and dashboard: not applicable before service activation
at the corresponding milestone in MISSION.md.

M2 / POC-F2-A3, REQ-ENG-004/007: `python services/model-gateway/http_gateway.py`
serves /embeddings and /rerank on 127.0.0.1:8010 (approved M2 schema).
Install requirements-dev.txt; the first run downloads pinned weights,
then uses local cache. CPU only, safetensors, no remote code allowed.
Engines: multilingual embeddings 384 dimensions and cross-encoder (Apache-2.0).
Dependencies: sentence-transformers 6.0.1 (Apache-2.0), torch CPU 2.14.0 (BSD),
several GB with weights; lighter ONNX alternative, separate integration.
Runbook: keep loopback, client timeout 30 s; explicit 400/413/502/504 errors.
Long inputs undergo tokenizer truncation (128/512 tokens).
Embeddings/rerank logs: cardinality and duration, no text; added cloud cost zero.
RAG SLO/dashboard not attested before integration; /answer remains to be implemented.

## M2 Generation
POST /answer follows contracts/m2-gateway.schema.json. Injected configuration:
SCW_GENERATIVE_BASE_URL (HTTPS), SCW_GENERATIVE_API_KEY, ESCALATION_MODEL.
The Scaleway L model receives delimited sources as untrusted data,
with versioned prompts/rag.txt. Provider timeout 25 s, 1024 tokens maximum,
reasoning_effort none, no automatic retry. Numerical references from the
model are resolved to the provided chunk_id; absence or invention is rejected.
HTTP 400 invalid input, 413 context exceeded, 502 provider/citation invalid,
504 timeout. Logs without text or keys: duration and number of citations.
Operational SLO: response or error bounded by the provider timeout; the target
RAG <12 s will be measured at the performance milestone. Serverless ceiling confirmed in
MISSION; no GPU created, billing by usage and not hourly.
Source: https://www.scaleway.com/en/docs/generative-apis/api-cli/using-chat-api/
Source: https://www.scaleway.com/en/docs/generative-apis/reference-content/supported-models/

## M4 Cascade
POST /agent/complete routes simple tasks to LOCAL_MODEL / LOCAL_API_BASE
(produced by infra/gpu-up.sh in BRAIN/gateway.env). Without a local endpoint,
the Scaleway fallback takes over; no GPU required at the M4 gate.
Contract: contracts/m4.md. Routing logs: class, provider, fallback; no text.
Runbook: inject M3 and local variables, launch the gateway then the harness.
A local failure consumes at most 2 s before L, within the M3 deadline. L price reserved
before call; L errors remain explicit. Local quality SLO to be measured in M6.
`make test-fallback` replays a recorded Scaleway response after a real local failure;
`make eval-routing` writes BRAIN/eval/routing.json. Bonus UI not delivered.

M7: /agent/complete accepts local_enabled=false for direct escalation without
GPU probing. observe=true adds provider (local/escalade) and route (simple/complexe)
to the response. Older clients retain their format and their M4 cascade.

M12: `/vision/complete` validates images with `packages/images.py`, reserves the
budget before inference, and uses `vision.yaml` for the sovereign model and its
rate. The transport performs neither image URL lookup nor text fallback.

M15: dev_gateway prepares an immutable code request and reserves UTF8 bytes +512 and maximum output under 50000 microEUR. Single TLS transport, no redirection or resumption; SSE bounded to 2 MB and mandatory terminal usage. Tests replay real Scaleway deltas (provider metadata removed), without external SDK dependency.


M21: public profiles atlas/atlas-glm/atlas-fast pin serverless text/code/fast roles
in routing.yaml. FAST_MODEL may override the configured fast model only when its
price/capabilities are present in the catalog. All three explicitly send
reasoning_effort=none; recovery and evaluation do likewise. Profile limits are
3000 output tokens,120 seconds and0.10 EUR. The gateway reserves full-context input
and at most two attempts before I/O, retains unknown usage, validates output and
returns provider_model and measured/estimated token splits. A budget-shortened
answer is held until validation; malformed output must not reach the user.
See contracts/m21.md for whole-evidence, activity and public HTTP acceptance.

M21 evaluation keeps explicit `reasoning_effort: none` for system and both judges.
The reference defaults to the configured fast model (`REFERENCE_JUDGE_MODEL` may
override it); system, production judge and reference must remain distinct. The
previous reference cannot disable reasoning on Scaleway. Recorded evaluations
match provider identity as well as role/messages, and calibration is reacquired
when the reference changes. Existing per-call/series budgets remain enforced.
