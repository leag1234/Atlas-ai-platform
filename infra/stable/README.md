# Stable ATLAS deployment kit

Follow [RUNBOOK.md](RUNBOOK.md) for the complete owner procedure. The front door
is the dedicated `atlas.indidea.org` sub-domain; `/atlas` is a static landing page.
Only an owner-approved, CI-green tag may be deployed. The instance uses base 100,
a separate secrets file, persistent volumes, and a restricted loopback SSH tunnel.

Every nginx proxy location includes `/etc/nginx/atlas-proxy-secret.conf` (outside
the checkout and web root, root:www-data, mode 640). nginx replaces guest identity
and role headers, terminates Basic authentication and forwards session cookies.
The relay validates the shared secret, removes it, and fixes the guest role to user.

The stable launcher configures the managed upload filter through the private
container endpoint before exposing the relay. Its reserved bootstrap account is
never available through the public identity domain. Stable mode disables UI code
execution and excludes model-authored terminal tools, including recovery calls.
Trusted presentation rendering remains available.

For disposable acceptance, export `ATLAS_PORT_BASE=1900`, set
`PATH="$PWD/.venv/bin:$PATH"` and `PYTHONPATH=.:tests:services/model-gateway`, then
run `python3 tests/stable_integration.py`. It uses nginx 1.18, real stack services,
two synthetic accounts and synthetic provider transport, with no inference spend.
The standalone browser harness takes its base URL and credentials from environment
variables; see its module documentation. Never run its destructive upstream check
against production: the disposable runner stops only its own tunnel stand-in.
