# edge-bff

Owner: `OWNERS`.

M0: local liveness only, contract `contracts/edge-bff.openapi.json`.
Runbook: `python3 services/edge-bff/server.py`, then
`bash services/edge-bff/healthcheck.sh`; stop with Ctrl-C.
SLO M0: `/health` returns 200 as long as the process is running.
Dashboard M0: JSON stdout logs (`service`, `event`); metrics M5.
