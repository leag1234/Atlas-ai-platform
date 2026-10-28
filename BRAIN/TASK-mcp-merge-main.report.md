# MCP merge resolution report

## Outcome

Resolved the merge of `origin/main` into `mcp-connectors` while retaining API-key
isolation, scoped MCP connectors, turn-local skills, user memory, and shared-project
roles. API-key requests neither authenticate or discover connectors nor declare or
execute legacy or brokered MCP tools. They receive `insufficient_scope` with the
missing `connectors` scope.

The regression in `tests/test_connector_selection.py` verifies that an API-key
context does not add connector declarations and rejects `mcp_call`, an `mcp_*` tool,
and `switch_connector_group`.

## Checks run

- Focused connector and API-key tests: passed (17 tests).
- Focused connector, skills, skill-delivery, and shared-project tests: run as part of
  the credential-free suite.
- `make lint`: passed.
- `make typecheck`: passed.
- `make verify-m16`: passed.
- `ATLAS_PORT_BASE=19200 make test`: completed; credential-free only.

## Cost

No live provider calls or cloud resources were used. Cost: 0 EUR.
