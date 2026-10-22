# M0 Contracts — to review and merge before implementation

Source: MISSION.md M0; docs/11 R-02, REQ-ENG-002/004/009/011.

`edge-bff.openapi.json` defines GET /health: HTTP 200, JSON `{"status":"ok"}`.
This liveness check does not depend on any provider and promises no GPU
availability. The server will listen by default on 127.0.0.1:8080; BFF_URL remains usable
by the existing healthcheck. Unknown paths will return HTTP 404.

Implementation PR plan, after human merge of this contract:
- minimal edge-bff server and skeletons of the services prescribed by docs/11;
- blocking lint/typecheck/test scripts, justified development dependencies;
- empty eval-harness, with no live calls, with an explicit count of zero cases;
- contract tests and real HTTP tests, unknown paths, test process termination;
- local `make verify-m0`, PR, green CI job, update to BRAIN/ then stop.

Planned acceptance tests: exact JSON compliance with the contract, real HTTP 200
via the healthcheck protected by verify-m0, HTTP 404 for an unknown path,
empty execution of the harness, and propagation of errors from quality tools.
The new tests will be shown failing before adding the server.

Excluded scope: M1+, GPU infrastructure, new UI, LLM calls, workflows, and verify-*.
Additional cloud cost: €0/h. No dependencies added by this PR.

## M0 Implementation
`make verify-m0` starts a temporary server on a free port, passes BFF_URL
to the protected script, then shuts down the server even if verification fails.
In CI, `make test` also calls this gate (keeps ATLAS_VERIFY_M0 anti-recursion).
`make eval-smoke` explicitly reports zero cases executed; `make eval` fails
until the executors are implemented. Golden datasets remain unchanged.

No runtime dependencies added. Development dependencies: Ruff 0.12.12
(MIT, lint/format) and mypy 1.17.1 (MIT, strict typing), versions pinned in
requirements-dev.txt. The stdlib suffices for HTTP and tests but does not replace
linting or strict typing REQ-ENG-003. These maintained tools, installed
only in dev/CI, do not increase the runtime image; wheels are a few MB.
Quality targets apply to M0 code and services; the pre-existing FLORES
generator is out of scope. Installation: virtual environment,
then `pip install -r requirements-dev.txt`.
