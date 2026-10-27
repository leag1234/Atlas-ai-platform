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
