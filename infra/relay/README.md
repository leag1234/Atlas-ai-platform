# Atlas relay

uptermd (github.com/owenthereal/upterm, Apache-2.0, v0.41.1, checksummed in the
Dockerfile) lets the users' own computers dial out to the instance; the runner
reaches them through it (services/runner/README.md, "Connected computers").

The serving stack starts it when `ATLAS_RELAY_PUBLIC_URL` is set
(services/orchestrator/serving.py, `relay()`): container `atlas-relay[-BASE]`,
WebSocket listener on `127.0.0.1:13740+BASE`, SSH listener only on the network
`atlas-relay-net[-BASE]` shared with the runner, host key made once in the
volume `atlas-relay-keys[-BASE]`, gate of enrolled host keys in the volume
`atlas-relay-gate[-BASE]` (written by the runner, read-only for the relay).
uptermd reads the gate once at start; the entrypoint restarts it within two
seconds of a change and connected computers register again under the same
session.

## Exposure

WebSocket through the existing HTTPS edge, not a new SSH port: computers on
office, hotel or school networks often reach only port 443, upterm uses
HTTPS_PROXY for wss, the certificate and the front door already exist, and no
firewall rule or new public port is needed. The SSH listener stays internal.

## Deploy (operator)

1. Build the images from the deployed worktree:
   `docker build -t atlas-relay:local infra/relay` and rebuild the runner image
   (`docker build --target runner -t atlas-project-runner:local -f infra/docker/Dockerfile .`).
2. Front door: inside the instance's HTTPS `server` block add

   ```nginx
   location /relay/ {
       proxy_pass http://127.0.0.1:13740;   # 13740 + port base
       proxy_http_version 1.1;
       proxy_set_header Upgrade $http_upgrade;
       proxy_set_header Connection "upgrade";
       proxy_read_timeout 1d;
       proxy_send_timeout 1d;
       proxy_buffering off;
   }
   ```

   then `nginx -t` and reload. `/api/atlas/connect.sh` and
   `/api/atlas/connect/*` must reach the chat adapter like the other `/api/atlas`
   routes (no login: the token and the signature authenticate them).
3. Instance environment: `ATLAS_RELAY_PUBLIC_URL=wss://HOST/relay/` and,
   when `ATLAS_PROJECT_ORIGIN` is not the address computers use,
   `ATLAS_RELAY_CONNECT_ORIGIN=https://HOST`. Optional mirror of the upterm
   archives (checksums stay in the script): `ATLAS_RELAY_DOWNLOAD`.
4. Restart the instance. Check: `docker logs atlas-relay-BASE` shows
   "starting server"; `curl -fsS https://HOST/api/atlas/connect.sh | head -3`.

Rollback: remove `ATLAS_RELAY_PUBLIC_URL` and restart; the tools and routes
disappear and existing SSH machines are unaffected. Connected computers stay
listed until their owners forget them.
