# User documents and Open Terminal

`make serve` starts the pinned Open Terminal container, authenticates using
OPEN_TERMINAL_API_KEY from the environment, and exposes it on 127.0.0.1:8000.
The launcher loads `../secrets.env` (or `ATLAS_SECRETS_FILE`), then optional `.env`
overrides. Keep both files outside Git. The default search provider is Tavily;
set `ATLAS_SEARCH_PROVIDER=serpapi` explicitly to select the fallback. Startup
checks the selected provider's credential before creating the terminal.
The Linux host needs Docker, nsenter, iptables and ip6tables; firewall setup
requires root or passwordless sudo and fails closed if unavailable.
The UI uses http://open-terminal:8000 on the shared internal atlas-files network.
Its runtime TERMINAL_SERVER_CONNECTIONS enables filesystem uploads.

Shell execution is a new model exposure surface. Docker provides containment:
only atlas-terminal-files is mounted at /home/user; never mount the repository,
Docker socket, host directories, or platform secrets. The container receives only
its own API key. It has no Scaleway, search-provider or model-provider credentials.
It runs without capabilities or privilege escalation, with a read-only root,
bounded memory/CPU/processes and temporary storage. IPv4 AND IPv6 OUTPUT rules
reject new outbound connections before the API starts. It cannot reach host
services 8010/8020/8030, metadata, other containers, or external networks. The UI
can initiate connections to the terminal; the terminal can only return replies.

The pinned upstream file API DOES allow traversal and symlink reads outside
/home/user, including /home/user/../../etc/hostname and a symlink to /etc/hostname.
The owner accepts this PoC risk (MISSION.md, 2026-09-18). API path checks do NOT
provide confinement. An authenticated caller can read disposable container system
files. Nothing sensitive may be mounted on the assumption that paths are checked.
This is a single-organization PoC, not a hardened multi-tenant file service.

Stop the launcher with Ctrl-C to stop the terminal and UI. Emergency stop:
`docker stop atlas-open-terminal`. The user-file volume persists; do not remove
it unless the owner requests deletion. An existing independently managed terminal
occupying port 8000 must be stopped before starting the stack; preserve its volume.
No GPU is required; incremental cloud resource cost is 0 EUR/h on the CPU VM.

Read PDF/DOCX/XLSX/PPTX/ODT/ODS/ODP/CSV/TXT/MD/images with installed Python libraries
and LibreOffice. Preserve sheets, columns, images and formulas. A scanned PDF,
password-protected document or extraction failure must be named explicitly.
Render PDF using soffice. For changes, state whether you regenerate or edit in
place: regeneration can discard formatting; edit in place preserves unrequested
styles, images and formulas. Never present a local path as a downloadable file.

Publication accepts paths relative to the current request directory and rejects traversal.
Already published downloads remain available if the closing inference fails.
