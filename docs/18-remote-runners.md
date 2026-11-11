# 18 — Remote execution targets for project Work

Status: proposed design for owner review, 2026-10-25. Nothing here is implemented. Requirement IDs are local to this design (`REQ-RUN-*`). It extends `docs/17-project-mode.md` (§5 Work loop, tools and approvals; §7 organization administration) and does not change its contracts.

## 1. Purpose and scope

Project Work already edits code and runs commands in a networkless, credential-free container (`services/orchestrator/code_workspace.py`, `services/orchestrator/code_worker.py`): 120 seconds and 512 MiB per command, 32 KiB of output. That is enough for unit tests of small Python or JavaScript projects. It is not enough for real software work:

- compilation that needs a toolchain, an SDK, more memory or more time (Android, C/C++, Rust, large Java builds);
- full test suites that take minutes;
- tests on a physical device: an Android phone attached to a server by USB, an iPhone attached to a Mac;
- execution in the target environment: Windows, macOS, a given Linux distribution or architecture.

This design lets a project work on a remote machine registered by the organization, over SSH, as a developer would in a terminal: the model runs its own commands there and gets back bounded summaries, full logs and the files it fetches.

REQ-RUN-01: P1 scope is SSH execution on organization-registered targets (Linux, macOS, Windows with OpenSSH Server), Android devices through `adb` on a target, and iOS simulators and devices through a macOS target. Out of scope: Atlas-managed cloud runners, autoscaling, runners that call back into Atlas, and desktop access to the user's own computer (the desktop locality work of `docs/17-project-mode.md` covers that separately).

## 2. Principles

1. The model never sees or handles a credential. It names a target and writes the commands to run; a broker holds the SSH key and opens the connection.
2. Atlas always initiates. A target never connects back to Atlas, so a compromised target cannot reach Atlas services.
3. Code leaves Atlas only by an explicit, recorded decision. Sending project code to a target is an egress like an external export (§5 of doc 17), recorded in the data ledger.
4. The model works on the target as a developer would in a terminal: it writes its own shell commands (build, run a test subset, read a log, install a dependency, drive `adb`), in a persistent working directory. What changes per target is the permission mode chosen by the admin (§6): ask for each command, allow by rules, or run without asking on a dedicated machine. Named command profiles are optional shortcuts, not the only way in.
5. Results are bounded in context and complete on demand. A short summary enters the model context; the full log and the artifacts are stored and reopened by reference (`reopen_result`).
6. Targets are disposable from Atlas's point of view. Each remote session works in its own directory, removed when the session ends. No secret is stored on the target by Atlas.

## 3. Domain model

REQ-RUN-02: new tenant tables in the project schema, with the common fields of doc 17 §2 (`org_id`, UUIDs, revisions, timestamps).

| Entity | Fields | Notes |
| --- | --- | --- |
| `runner_target` | `id`, `org_id`, `name`, `os` (`linux`, `macos`, `windows`), `arch`, `host`, `port`, `user`, `jump_target_id` (optional), `host_key_fingerprints[]`, `credential_ref`, `shell` (`posix`, `powershell`), `work_root`, `capabilities[]`, `permission_mode` (`ask`, `rules`, `auto`), `allow_rules[]`, `deny_rules[]`, `max_parallel_sessions`, `status`, `last_probe` | Registered by an organization admin. `host_key_fingerprints` are pinned at registration; any mismatch fails closed. `jump_target_id` reaches a machine on a private network through another registered target (OpenSSH `ProxyJump`), for example a Mac on the same LAN as a reachable server. |
| `runner_profile` | `id`, `target_id`, `name`, `command`, `description`, `timeout_s`, `artifact_globs[]`, `max_artifact_bytes`, `needs_device` | Optional shortcuts documented for the model, for example `apk` → `./gradlew assembleDebug` with artifacts `app/build/outputs/apk/**/*.apk`. A profile is a command like any other for permissions. |
| `runner_device` | `id`, `target_id`, `kind` (`android`, `ios_device`, `ios_simulator`), `serial_or_udid`, `label`, `status` | Discovered by the target probe (`adb devices`, `xcrun simctl list`, `xcrun devicectl list devices`). |
| `runner_grant` | `target_id`, `project_id`, `granted_by`, `code_egress_approved_at` | A project may use only the targets granted to it. The first use of a target by a project requires the code egress approval (§6). |
| `runner_session` | `id`, `org_id`, `project_id`, `work_session_id`, `target_id`, `remote_dir`, `synced_digest`, `state`, `opened_at`, `closed_at` | One remote working directory per Work session and target, kept between commands and turns, removed at close or after 24 hours idle. |
| `runner_command` | `id`, `runner_session_id`, `command`, `cwd`, `device_id`, `decision` (`auto_rule`, `auto_mode`, `approved`, `denied`), `state`, `exit_code`, `started_at`, `ended_at`, `bytes_sent`, `bytes_received`, `log_file_id`, `artifact_file_ids[]` | States: `awaiting_approval`, `running`, `succeeded`, `failed`, `timed_out`, `cancelled`, `unknown`. |

Capabilities are declarative tags used for selection and display (`build`, `test`, `android`, `ios`, `gpu`, `windows-desktop`). They grant nothing by themselves.

## 4. Components

REQ-RUN-03: a new internal service, the runner broker (new `services/runner/`), is the only component that holds SSH keys and opens SSH connections.

- **Credentials.** Private keys are stored outside the database, in a secret directory mounted only into the runner broker, and referenced by `credential_ref`, in the same way as `ATLAS_FORGE_CONNECTORS` for the forge broker. The admin API accepts a public key fingerprint and an opaque reference, never a private key in a browser form. An admin can also ask Atlas to generate a key pair: the broker keeps the private key and shows the public key to install on the target. Keys are per target, ed25519, and can be rotated without changing sessions.
- **Network.** The broker container's egress is limited to the registered `host:port` pairs. The Work worker remains networkless, as in doc 17.
- **Authorization.** The broker accepts requests only from the orchestrator, with the scoped identity and execution secret already used by `/internal/atlas/execution/{operation}`. It checks the grant, the profile, the approval and the quotas again; the orchestrator's checks are not trusted alone.
- **SSH client.** OpenSSH client run as a subprocess with `StrictHostKeyChecking=yes`, a per-target `known_hosts` file generated from the pinned fingerprints, `BatchMode=yes`, no agent forwarding, no port forwarding, no X11, `ServerAliveInterval` and a connect timeout. The environment is cleared, as `services/orchestrator/project_forge.py` does for git.

## 5. Remote sessions and commands

REQ-RUN-04: a remote session opens on the first command a Work session sends to a target, and keeps its directory between commands and turns, like a terminal.

1. **Sync in.** Before each command, the broker brings `remote_dir` to the current workspace digest: first time, a git bundle of the scratch repository copied with `scp` and cloned; afterwards, an incremental bundle of the new commits (the broker commits the scratch workspace internally). What is sent is exactly the digest recorded on the command. Files created on the target by builds stay there (build caches survive between commands).
2. **Run.** The command runs in `remote_dir` (or a subdirectory), in its own process group (POSIX `setsid`; Windows a job object through a small PowerShell wrapper), with a timeout (default 10 minutes, at most 30 in P1), and output streamed back. Shell state that matters (current directory, exported variables set through a project `.atlas/remote.env` file) is explicit, not hidden between commands.
3. **Progress.** Output lines stream to the Work view. Long commands do not block the model: it can continue and call `remote_status`.
4. **Sync out.** Nothing flows back implicitly. The model fetches what it needs: `remote_fetch` for a file or glob (bounded size), stored as project files with ledger events. Changes to source files are made in the Atlas workspace, then synced in; a file the model wants from the target (generated code, a lockfile) is fetched and written explicitly.
5. **Close.** At Work session end, on explicit close, or after 24 hours idle, the broker removes `remote_dir`; a periodic sweep removes leftovers.
6. **Cancel.** User cancel, turn cancel or timeout kills the remote process group. If the connection is lost, the command becomes `unknown`; the broker reconnects, checks the process group and settles the state before anything else. It never retries automatically.

Limits: concurrency per target and per organization, monthly runner minutes, disk quota per remote session (checked by the probe and before sync). Remote commands count against the Work turn's tool and time ceilings (doc 17 §5); a command that outlives the turn stays attached to the session and its result is available to the next turn.

## 6. Permissions and approvals

REQ-RUN-05: three permission modes per target, set by the organization admin, with the same broker-enforced approvals as doc 17 §5 (bound to organization, member, project, session, exact command and code digest, target, expiry; consumed once).

| Mode | Behaviour | Intended for |
| --- | --- | --- |
| `ask` | Every command is shown verbatim and waits for approval. The approval card offers "allow once" and "always allow commands starting with ..." (adds a project-level rule after confirmation). | Shared or sensitive machines. |
| `rules` (default) | Commands matching an allow rule run directly (for example `make *`, `./gradlew *`, `python -m pytest *`, `adb -s * shell *`, `ls *`, `cat *`); others ask; deny rules always refuse (for example `sudo *`, `rm -rf /*`, `curl * | sh`). Rules match the parsed command, including each part of a pipeline or `&&` chain. | Build and test servers. |
| `auto` | Every command runs without asking, except deny rules. | A dedicated, disposable machine. |

Always required whatever the mode: the first use of a target by a project (code egress, showing the target and what will be sent, recorded as `runner_code_egress_approved`, revocable), and actions declared sensitive by the admin (for example uninstalling an app or wiping data on a device). Policy denials cannot be approved away. Projects whose files are local-only (doc 17 §12, answer 5) cannot send code to a target unless the admin marks the target as inside the same locality.

## 7. Model-facing tools

REQ-RUN-06: a new Work tool group `remote`, within the group mechanism of `services/orchestrator/work_tool_selection.py` (at most four group tools in context).

| Tool | Arguments | Returns |
| --- | --- | --- |
| `list_targets` | optional `capability` | Granted targets: name, OS, arch, shell, capabilities, permission mode, profiles, devices, status. No host, user or key information. |
| `remote_run` | `target`, `command`, optional `cwd`, `device`, `timeout_s`, `wait_s` | Command ID and state; when it finishes within `wait_s`, the summary below. The command is the model's own shell command; permissions follow §6. |
| `remote_status` | `command_id`, optional `wait_s` | State, elapsed time, last lines; on completion the summary. Also cancels with `cancel: true`. |
| `remote_fetch` | `target`, `path_or_glob` | Stored project files (references), bounded in count and size. |

Summary returned to the model, at most 8 KiB: exit code, duration, test counts when a JUnit or TAP report is found, the first failures, the last output lines, and references to the full log and any fetched artifacts. Credential patterns are redacted, as in Code mode (`services/orchestrator/README.md`). Output and files from the target are untrusted data, like any tool output.

## 8. Target types

REQ-RUN-07: one transport (SSH), with differences declared per target, not coded per customer.

- **Linux.** POSIX shell, `setsid` process groups. Recommended: a dedicated unprivileged account, no sudo, and container builds (Docker or Podman rootless) when isolation between sessions is needed.
- **macOS.** POSIX shell. Xcode builds (`xcodebuild`), iOS simulators (`xcrun simctl`), physical iOS devices (`xcrun devicectl`). Signing identities stay on the Mac under the admin's control; Atlas never transfers them.
- **Windows.** OpenSSH Server with PowerShell as the default shell, Git for Windows, profiles written in PowerShell. A small wrapper script (shipped by Atlas, installed by the admin) provides process-group kill and exit codes.
- **Android devices.** A Linux or macOS target with `adb` and the phones attached by USB. The probe lists devices by serial. The model drives devices with `adb -s <serial> ...` commands: install, instrumented tests (`connectedAndroidTest` or `am instrument`), `screencap` fetched as a PNG, bounded `logcat`. One remote session per device at a time (device lock in the broker).
- **iOS devices.** Through a macOS target, as above.

Probe: at registration and every 15 minutes, the broker connects, verifies the host key, and reads OS version, architecture, free disk in `work_root`, git version and devices. The admin UI shows the result; a target whose probe fails is `unavailable` and `remote_run` fails visibly with that status, never as a generic error.

## 9. Audit and ledger

REQ-RUN-08: data ledger events (doc 17 §6) extended with `runner_code_egress_approved/revoked`, `runner_session_opened/closed`, `runner_command_requested/approved/denied/started/completed/failed/cancelled/unknown`, `runner_file_fetched`. Each event carries target ID, the command text digest and decision, code digest, device ID, exit code, durations and bytes in each direction. The organization audit view lists commands per target and per member, with the command text. No log content is copied into the audit record.

## 10. Threat model

| Threat | Mitigation |
| --- | --- |
| Prompt injection in repository files or logs makes the model run harmful commands on a target | Permission mode per target (`ask` or `rules` by default, `auto` only on dedicated machines); deny rules; commands shown verbatim; unprivileged account; per-session directory; recommended container builds. |
| Credential exfiltration | Keys only in the runner broker; never in the worker, the model context, logs or artifacts; output redaction; no agent forwarding. |
| Target impersonation | Pinned host key fingerprints, `StrictHostKeyChecking=yes`, no trust on first use after registration. |
| Compromised target attacks Atlas | No connection from target to Atlas; artifacts stored as untrusted files with size limits and MIME checks; logs treated as untrusted data. |
| Cross-project leak on a shared target | Per-session directory removed at close; device lock; recommendation of containers or one account per project for sensitive organizations. |
| Unbounded cost or occupation | Command timeouts, concurrency, minutes and disk quotas, explicit cancel, sweep of stale session directories. |

## 11. Ordered implementation tasks

| Order/size | Scope | Tests/acceptance | Dependencies |
| --- | --- | --- | --- |
| R-A / S | Contract: tables, admin API, tool schemas, ledger events; new `contracts/remote-runners.md` | Schema examples validate | Owner approval of this design |
| R-B / M | Registry and admin API, credential references, key generation, host key pinning, probe; new `services/runner/registry.py`, `tests/test_runner_registry.py` | Org isolation matrix, fingerprint mismatch fails closed, keys never returned by any API | R-A |
| R-C / L | Broker execution on Linux: remote sessions, incremental bundle sync, process groups, streaming, timeout, cancel, cleanup, unknown-state reconciliation, permission modes and rule matching; new `services/runner/broker.py`, `tests/test_runner_broker.py` | Against a real disposable SSH target in a container in CI; kill and reconnect cases | R-B |
| R-D / M | Work tool group `remote`, approvals, progress events, UI cards and admin page; `services/orchestrator/work_tool_selection.py`, `services/orchestrator/code_tools.py`, `services/orchestrator/chat-ui.js`; new `tests/test_project_remote_tools.py` | Browser acceptance: failing test fixed in Work, the model runs the suite and its own diagnostic commands on a Linux target, approval cards in `ask` mode, cancel | R-C |
| R-E / S | Logs and artifacts as project files, JUnit and TAP parsing, `reopen_result` references | Large logs, artifact size limits, hashes | R-D |
| R-F / M | Android through `adb`: device discovery, lock, install, instrumented tests, screenshots | Emulator in CI; owner acceptance on the owner's server with three attached phones | R-E |
| R-G / M | Windows target: PowerShell wrapper, Git for Windows bundle clone | Owner acceptance on a Windows machine | R-E |
| R-H / M | macOS target, iOS simulator and device | Owner acceptance on the owner's Mac, reached through `jump_target_id` | R-E |

First live acceptance: the existing test VM registered as a Linux target, then the owner's Android server and Mac, the demo repository of the code trial, the suite run remotely, then a change approved and committed through the forge broker.

## 12. Owner answers (2026-10-25)

1. Registration: organization admins only in P1; project owners registering machines they control comes later.
2. Direct SSH is enough for P1 (with `ProxyJump` through another registered target for machines on a private network); a reverse mode for machines behind NAT is deferred.
3. The model writes its own commands on targets; the safety lever is the per-target permission mode (§6), not a closed list of commands.
4. Acceptance targets: the test VM (Linux), the owner's server with attached Android phones, the owner's Mac. Windows when a machine is available.

## 13. Personal machines (owner decision, 2026-11-11)

This section supersedes owner answer 1 of section 12 for machines a member administers himself. Organization targets are unchanged: an administrator registers them and grants them per project (sections 3 to 6).

Reason: an administrator step for a personal server blocks ordinary use. A developer who can reach a server over SSH uses it directly with other coding assistants; Atlas does the same, without weakening principle 1 (the model never handles a credential).

REQ-RUN-09: any active member declares his own machines, with no administrator and no project grant.

Flow (`/api/atlas/orgs/{org}/machines`, contract `contracts/project-mode.openapi.yaml`):

1. **Declare.** The member gives a name, host, port, user and permission mode (`rules` by default; `ask` and `auto` as in section 6). The runner broker generates one ed25519 key pair for this machine and keeps the private key. The response carries the public key and a ready-made command to run in the member's own terminal: `ssh USER@HOST 'mkdir -p ~/.ssh && echo "<public key> atlas" >> ~/.ssh/authorized_keys'` (with `-p PORT` when the port is not 22).
2. **Verify.** On the member's request Atlas connects. The host key seen at the first verification is pinned and shown to the member; later connections fail closed on any other key, until the member explicitly resets the pin (reinstalled server). The probe of section 8 then checks the account. The work root defaults to `atlas-work` in the account's home.
3. **Use.** Once verified, the machine is listed by `list_targets` (marked `personal`) in every Work session of its owner: each project where he works and the hidden chat workspace. The per-project record of use (code egress approval, "always allow" rules) is created on first use.
4. **Manage.** The member lists, renames, changes the mode, verifies again and deletes. Deleting removes the private key, the pinned host key and the session directories that can still be reached.

Ownership rule: a machine has one `owner_member_id`. No other member, administrators included, can read, change, verify, delete or use it; the API answers 404, as for any resource outside the caller's scope (doc 17). The rule is enforced three times: by the retrieval service on every route and Work operation, by row-level security on `runner_targets` (the owner column is part of every policy), and by the broker, which refuses a control permit or a command whose member is not the owner.

Security reasoning:

- **One Atlas key per machine.** The member's own SSH key never leaves his computer and is never given to Atlas. Atlas holds a key that opens this account on this machine only.
- **Revocation by one line.** Removing the `atlas` line from `authorized_keys` ends Atlas access at once, whatever the state of Atlas. Deleting the machine in Atlas destroys the private key.
- **Strict input.** Host, user and port are accepted by allow-list patterns (DNS name or IPv4 literal, POSIX account name, 1 to 65535), never escaped. The installation command is built from those validated values only.
- **No reach into the deployment.** A member chooses the host, so the broker resolves it and refuses any address that is not public, unless the operator listed its network in `ATLAS_RUNNER_PERSONAL_NETWORKS` (comma-separated CIDR, for on-premises machines). The broker then connects to the vetted address.
- **Contact only on request.** Atlas connects to a personal machine when its owner verifies or uses it; the periodic probe of section 8 does not apply.

Code egress: the first-use approval of section 6 is kept for personal machines, once per project and machine, decided by the owner himself. Whether declaring and verifying a machine is already that consent (at least in the member's private chat workspace, where no approval card exists yet) is an open owner decision.

Limits in this step: Linux and macOS accounts reached directly (no jump host), 20 machines per member, default quotas (2 parallel sessions, 10 GiB per session directory), files marked local-only never leave. Not included: a settings page, approvals inside the chat conversation, a local helper, depositing an existing private key.

### 13.1 Setup in the conversation (owner decision, 2026-11-11)

There is no machines page: a machine is set up by talking to Atlas ("I have a server at 192.0.2.10, use it over SSH to compile, and another one at 198.51.100.7 for the tests"). Any setup step happens in the conversation: the assistant asks, does, shows the command with a copy button, verifies, then continues the task.

Work tools (`services/orchestrator/work_machine_tools.py`), in the `remote` tool group, act as the member through the routes above (`ExecutionBroker.member`, his own identity, an Idempotency-Key on every change and If-Match on rename, mode change and removal):

| Tool | Arguments | Returns |
|---|---|---|
| `machine_add` | `name`, `host`, `user`, `port` (22), `permission_mode` (optional) | the machine, `install_command` verbatim, a sentence for the person, the meaning of its permission mode |
| `machine_verify` | `name`, `reset_host_key` (false) | status and host key fingerprint, or one sentence per failure (key not installed, with the command again; host key changed, with the risk; private address; unknown host name; no SSH connection) |
| `machine_list` | none | name, address, status, permission mode; no credential |
| `machine_change` | `name`, `new_name`, `permission_mode` | the changed machine |
| `machine_forget` | `name`, `confirmed` | refused unless `confirmed` is true; the model sets it only after the person said yes |

Machines are named by the person or by Atlas after their use (`build`, `tests`); a request may declare several.

Flow across turns:

1. The request is routed `remote` (or matches the remote phrasing without the router). Work starts in the `remote` group even when the member has no machine yet; the machine rules are added to the Work system text. `machine_add` is called once per machine, the answer shows each command in a `bash` block and the turn ends. Verifying a machine declared in the same turn returns at once without connecting (its key cannot be installed yet). Whatever the model writes, every install command of the turn is in the final answer, verbatim, in a code block.
2. The next message ("done", "c'est fait", or the error the person got) goes back to the conversation's Work session: the chat sees an install command in the last answer and treats the reply as a follow-up unless the router classified it as research, a drawing, an app or a file. Work starts in the `remote` group again, verifies, and continues the original request from the conversation history. A machine verified in the turn makes the remote commands available at once.
3. Later conversations use a verified machine by name through `list_targets` and `remote_run`, without setup.

Permission mode: no approval card can be answered in the chat yet. In the chat a new machine therefore uses `auto`, and the answer says that commands will run on that machine without asking each time and that the person can refuse; if he does, the mode becomes `rules`, and the answer says that commands on that machine will then not run in the chat. In a project, where approvals are shown, the backend default (`rules`) applies. Together with section 13's code egress rule, declaring and verifying a machine in the private chat workspace is the consent to send code there.

Instructions to the model, in short: never ask for a password or a private key; never ask the person to run anything on the server other than the install command; diagnose read-only first; relay each sentence in the person's language, never an error code; ask before resetting a changed host key; remove a machine only after an explicit yes.
