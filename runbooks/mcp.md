# MCP GitLab local — M14

Source: contracts/m14.md, REQ-MCP-001..004.
Install the pinned MIT adapter:
`npm install --prefix /opt/atlas-mcp --ignore-scripts @zereight/mcp-gitlab@2.1.60`.
The test installation occupied 41 MB, package 2.24 MB excluding dependencies; Node 22.23.2,
transitive MCP Node SDK 1.30.0. Adapter maintained publicly; no new Python dependencies.
Alternative studied: Python SDK 2.2.0 (366 KB excluding dependencies),
not retained to avoid unused transports in this bounded stdio client.

Copy `infra/mcp/gitlab.json` to a private file, replace the GitLab URL and
REPLACE_PROJECT_ID, then define ATLAS_MCP_CONFIG with its path. Provide
ATLAS_GITLAB_TOKEN via private environment (never in a command, config, or
repository). Use an API token limited to the authorized project and necessary permissions.
The list of tools, their classification, and their fixed arguments are administrative.
Do not allow fields enabling replacement of endpoint, project, or credentials.
Executable MCP servers are trusted programs installed by the admin.

Restart `make serve`. With the existing SSH tunnel for 3000 and 8020, request a
GitLab read or an issue creation. For a write operation, open the local link 8020,
verify the displayed arguments, then click "Confirm and execute once".
No effect on GET. Authorization valid for 10 minutes, single-use, linked to the config
and token; maximum 100 requests. Process restart = invalidation of pending actions.
Single-user UI, single process; no public exposure of this confirmation.
If the result indicates an uncertain write, verify GitLab before a new
request; ATLAS never automatically retries a write.

Without ATLAS_MCP_CONFIG, the four existing tools remain unchanged.
Log: BRAIN/mcp/actions.jsonl, permissions 0600, names/status/duration only.
No provider stderr, content, or arguments in this log.
MCP returns are untrusted data; known secret returned = refusal.
MCP timeout 15 s, arguments 16 KiB, response 256 KiB; protocol version 2025-11-25,
text only, no sampling, roots, elicitation, tasks, or HTTP transport.

`make verify-m14` replays versioned synthetic real exchanges in CI.
To repeat the real gate on a test GitLab: configuration under the name gitlab,
issue 1 described exactly as "Synthetic fixture: expected colour is blue.", then
ATLAS_M14_MODE=live. The gate creates a test issue after HTTP confirmation and
replaces the cassette; a disposable synthetic project is required, never enterprise.
The four indicators are written only after assertions and adversarial tests.
Estimated MCP cost 0 EUR per request for this API without per-call billing; LLM cost
remains within the existing budget. No GPU required.
