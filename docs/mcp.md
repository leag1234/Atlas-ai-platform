# MCP client

Atlas can call administrator-configured MCP tools from the chat assistant.
It is a **stdio client subset**: JSON-RPC initialization, ping and synchronous
`tools/call`, protocol version `2025-11-25`, with text results. It does not expose
an MCP server or support HTTP transport, resource/prompt discovery, sampling,
roots, elicitation or background tasks. Tools are configured explicitly rather
than discovered through `tools/list`.

## Configure a server

Install a trusted server on the application host. The repository's GitLab example
uses Node 22 and a pinned adapter:

```bash
npm install --prefix /opt/atlas-mcp --ignore-scripts @zereight/mcp-gitlab@2.1.60
```

Create a private JSON file and set `ATLAS_MCP_CONFIG` to its absolute path. This
minimal example permits reading issues from one project:

```json
{
  "gitlab": {
    "command": "/usr/bin/node",
    "args": ["/opt/atlas-mcp/node_modules/@zereight/mcp-gitlab/build/index.js"],
    "env": {
      "PATH": "/usr/bin:/bin",
      "GITLAB_API_URL": "https://gitlab.example.com/api/v4",
      "GITLAB_DISABLE_VERSION_CHECK": "true",
      "GITLAB_PERMISSION_MODE": "modify",
      "GITLAB_DENIED_TOOLS_REGEX": "^(?!(get_issue)$).*"
    },
    "secret_env": {"GITLAB_PERSONAL_ACCESS_TOKEN": "ATLAS_GITLAB_TOKEN"},
    "tools": {
      "get_issue": {
        "effect": "read",
        "fields": ["issue_iid"],
        "fixed": {"project_id": "REPLACE_PROJECT_ID"}
      }
    }
  }
}
```

Replace the public URL, project identifier and executable paths for your deployment.
Provide `ATLAS_GITLAB_TOKEN` through the private environment, with permissions scoped
to that project. `secret_env` maps server variable names to host variable names,
never token values. The subprocess receives only `env` and resolved `secret_env`;
provide any necessary executable search path explicitly.

`fields` allows model-supplied arguments. `fixed` locks arguments such as project
identity; attempts to override them are rejected. Keep server endpoints,
credentials and executable selection under administrator control. Server programs
run on the application host, so install only trusted programs.

Restart `make serve` with the private configuration loaded. Ask, for example,
“Read issue 1 from the configured GitLab project.” Atlas exposes one `mcp_call`
tool to the model and applies the configured server/tool policy. Without
`ATLAS_MCP_CONFIG`, no MCP tool is added.

## Writes and confirmations

The [full example](../infra/mcp/gitlab.json) adds `create_issue` with
`effect: "write"`, permits `title` and `description`, fixes the project and expands
the adapter's allowlist. Review those permissions before enabling it.

For a write request, Atlas returns a local confirmation link. Forward the adapter
port over SSH as described in [installation](installation.md), open the link,
review the server, tool and arguments, then click **Confirm and execute once**.
Opening the page does not execute the write.

Approval expires after ten minutes, is single-use and is bound to the arguments,
server configuration and credentials. Restarting the process invalidates pending
approvals. At most 100 confirmations can be pending. Keep this confirmation
interface local to the owner; it is not a public multi-user authorization system.
An uncertain write is never automatically retried: inspect the remote system
before issuing another request.

## Bounds and results

Calls have a maximum 15-second timeout, a 16 KiB call/argument envelope and a
256 KiB protocol-response bound. Configuration is limited to 64 KiB. MCP output
is treated as untrusted data; echoing a configured secret is rejected. Audit records
contain server/tool names, state and duration, without arguments or result content;
`ATLAS_MCP_LOG_DIR` selects their private destination.

Source: [configuration and dispatch](../services/orchestrator/mcp_client.py),
[stdio transport](../services/orchestrator/mcp_transport.py),
[confirmations](../services/orchestrator/mcp_confirmation.py) and
[adapter installation record](../runbooks/mcp.md).
