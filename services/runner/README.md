# POSIX remote runner broker

REQ-RUN-01–08; contract: `contracts/remote-runners.md` (approved by the supervisor).

Retrieval owns registration, project grants, approvals and tenant records. The
orchestrator uses the existing execution identity/secret and freezes a canonical
scratch snapshot. Retrieval signs a short-lived permission for the runner. The
runner verifies the signature, grant, policy, exact command/snapshot and consumed
approval, then executes OpenSSH with a clean environment. It has no project DB
connection. Work workers retain their existing networkless container boundary.

The project Compose overlay adds a separate runner image, `/keys` and `/state`
volumes. Only this service mounts SSH keys. The operator runs the existing
`upgrade_work` migration explicitly; runtime startup never changes the database.
The additive SQL is `services/retrieval/project_runner_schema.sql`. Do not roll
back while commands are active: close sessions, remove the runner service, then
remove the four runner tables and `runner_usage(uuid)` if abandoning the feature.
Existing project tables and file results remain valid.

In Project mode, organization admins open **Remote targets**, generate an
ed25519 key pair, install the once-displayed public key in a dedicated unprivileged
account, enter its opaque reference/public fingerprint and independently verified
host-key fingerprints, and register/probe the target. Grant it to the selected
project. Admin access to project content still requires an explicit project grant.
Private keys can also be installed by the operator at `/keys/<org>/<reference>`
with mode 600; no private key browser form exists. Rotate by generating a new
reference and updating the target. Host/user/root/jump cannot move while sessions
are open. A jump account must allow forwarding only to the intended destination
and have an OpenSSH client for pinned scanning; no reverse connection is used.

Linux/macOS prerequisites: Python 3, Git, OpenSSH/SFTP, POSIX shell and process groups.
The target work root must exist and be writable. The broker connects only to
registered endpoints. Its container firewall permits established responses, the
Docker resolver, and those SSH pairs; the API accepts traffic on its control
interface. Put isolated test targets on a second internal network. Targets never
receive the broker's private keys, service secrets, provider keys or Docker socket.

Register macOS with `os: macos`; the probe verifies Darwin and the helper uses
`ps` process start times for reconciliation. Linux retains procfs process identity.
Use a dedicated account and keep SDK caches inside its session workspace. Android
commands can use an already running local adb server; device access and any
device changes remain subject to the organization's command rules and consent.

The first egress and exact command share an approval card. `ask` asks for commands;
a confirmed project prefix rule can allow that prefix subsequently. `rules`
automatically permits matching parsed argv components. `auto` is for dedicated
disposable targets. Deny and sensitive rules take precedence. Unsupported shell
expansion, redirection or command indirection is rejected explicitly; put complex
logic in a reviewed project script. Rules granting interpreters authorize their
argument programs. This is command consent, not a sandbox on the target account.

Remote directories and untracked build caches persist across turns. Initial Git
bundles and incremental commits sync only validated source snapshots. A detached
watchdog streams bounded logs and kills the POSIX process group on timeout or
cancel. Unknown outcomes are reconciled without rerunning commands. Active
commands are polled after the Work turn so full redacted logs become project files.
Logs stop execution at 16 MiB; fetched files are capped at 20 files/16 MiB and
checked for traversal/symlinks, size and hash. JUnit/TAP summaries are bounded.
Text results have `reopen_result` references, split into indexed segments when
large. Fetched files are untrusted and never implicitly replace workspace sources.

Default timeout is 600 seconds, maximum 1800. Target concurrency/disk/minutes
limits and an organization ceiling of 16 sessions/3000 minutes apply. Running and
unknown commands reserve their timeout; completed commands account their duration.
The broker probes known targets every 15 minutes and sweeps idle directories at
24 hours. Failed cleanup preserves its checkpoint and reports unknown; explicit
cancel/revocation closes sessions. No GPU or paid resource is provisioned.

Focused credential-free checks (choose a free host port base):

```sh
PYTHONPATH=services/model-gateway:services/edge-bff:.:tests ATLAS_PORT_BASE=18600 \
  python3 -m unittest tests.test_runner_broker tests.test_runner_registry tests.test_project_remote_tools -v
make lint typecheck
make test
ATLAS_PORT_BASE=19600 make test-journeys
```

The SSH tests build `tests/runner/Dockerfile`, use a disposable unprivileged SSH
account and random loopback host port, then remove the container. New operating
system packages are OpenSSH client and iptables in the runner image, and OpenSSH
server/Git/util-linux in the disposable target. They provide the required standard
transport/firewall/process-group tools; no Python dependency is added. Their
upstream distribution licenses and maintenance are inherited from Debian.
