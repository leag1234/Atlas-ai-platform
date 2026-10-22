# Stable Atlas owner runbook

These are owner operations, not commands executed during integration acceptance.
Use the three parts in order. The VM runs Atlas; the web server runs nginx and SSH
only. Production uses base 100 (UI 3100, terminal 8100, gateway 8110, retrieval
8111, adapter 8120). Never publish those ports on an external interface. Reserve
base 1900 for the release checks; stop if another task owns it.

Keep shell tracing off (`set +x`). Never paste secrets into commands, chat, Git,
or a web directory. Shell variables below contain public host names and usernames
only. Commands that prompt for credentials do not echo them. Every command block
states its machine, user and expected result. Replace the public placeholders once.
A root process can read Docker credentials and data: root and Docker membership
are privileged access, not a guest isolation boundary.

## A. VM preparation and first deployment

1. **VM, root, working directory `/opt/atlas-src`.** Select a reviewed release tag
   whose commit has a green GitHub CI run. The checkout below is the owner's release
   source. Do not run this procedure while another agent is modifying it.

   ```bash
   cd /opt/atlas-src
   set +x
   umask 077
   export TAG=stable-YYYYMMDD-N
   export VM_HOST=VM_PUBLIC_IP
   export WEB_OWNER=OWNER_LOGIN
   export WEB_SERVER_IP=WEB_SERVER_PUBLIC_IP
   git -C repo fetch --tags origin
   git -C repo show-ref --verify "refs/tags/$TAG"
   gh -R leag1234/atlas run list --commit "$(git -C repo rev-parse "refs/tags/$TAG")" --status success
   ```

   Expected: the exact tag commit and a successful **CI** workflow URL matching that
   commit. Review the run and required jobs, not merely a successful unrelated job.
   Do not create the marker until that review is complete. This runbook contains no
   claim that the integration branch has CI approval.

2. **VM, root.** Prepare the Python runtime and private state. The installed launcher
   runs as root because it must enter the terminal network namespace and install
   firewall rules. It does not grant host privileges to the terminal container.

   ```bash
   cd /opt/atlas-src
   apt-get update
   apt-get install -y python3-venv git openssh-server rsync curl iptables util-linux
   docker version
   install -d -o root -g root -m 700 /opt/atlas-src/stable-kit
   git -C repo archive "refs/tags/$TAG" infra/stable | tar -x -C stable-kit --strip-components=2
   git -C repo show "refs/tags/$TAG:requirements.txt" > /opt/atlas-src/stable-requirements.txt
   python3 -m venv /opt/atlas-src/stable-python
   /opt/atlas-src/stable-python/bin/pip install -r /opt/atlas-src/stable-requirements.txt
   install -d -o root -g root -m 700 /var/lib/atlas-stable
   install -d -o root -g root -m 700 /var/lib/atlas-stable/logs
   install -d -o root -g root -m 700 /var/lib/atlas-stable/images
   ```

   Expected: Docker is available; dependencies install with the repository's Python
   version. Runtime files are root:root, directories mode 700 under the current umask.
   Provider/model artifacts must be available to this runtime before serving guests.
   Do not start the owner development stack to prepare them.

3. **VM, root.** Create `/opt/atlas-src/stable.env` exclusively (root:root, 600).
   Provider values are requested privately; model routing comes from the gateway.
   Use dedicated provider keys with owner-configured spending caps and alerts.

   ```bash
   cd /opt/atlas-src
   /opt/atlas-src/stable-python/bin/python3 - <<'PY'
   import getpass, os, secrets, shlex, subprocess
   from pathlib import Path
   import yaml
   values = {entry['env']: entry['model'] for entry in
             yaml.safe_load(subprocess.check_output(['git', '-C', 'repo', 'show',
                 'refs/tags/' + os.environ['TAG'] + ':services/model-gateway/routing.yaml']))['serverless'].values()}
   for key in ('SCW_GENERATIVE_BASE_URL', 'SCW_GENERATIVE_API_KEY', 'TAVILY_API_KEY'):
       values[key] = getpass.getpass(key + ': ')
       if not values[key].strip():
           raise SystemExit('Missing required value: ' + key)
   values.update({key: secrets.token_hex(32) for key in
                  ('WEBUI_SECRET_KEY', 'ATLAS_PROXY_SECRET', 'OPEN_TERMINAL_API_KEY')})
   values.update(ATLAS_STABLE='1', ATLAS_PORT_BASE='100', GPU_LOCAL='0',
       ATLAS_TRUSTED_EMAIL_HEADER='X-Atlas-User', ATLAS_SEARCH_PROVIDER='tavily',
       ATLAS_HEALTHCHECK_USER='healthcheck@atlas.indidea.org',
       ATLAS_SECRETS_FILE='/opt/atlas-src/stable.env',
       ATLAS_INTERACTION_DIR='/var/lib/atlas-stable/logs',
       ATLAS_WEB_CACHE='/var/lib/atlas-stable/web.sqlite',
       ATLAS_IMAGE_DIR='/var/lib/atlas-stable/images',
       ATLAS_PROJECT_DB='/var/lib/atlas-stable/projects.sqlite',
       ATLAS_PUBLIC_URL='https://atlas.indidea.org')
   fd = os.open('stable.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
   with os.fdopen(fd, 'w') as output:
       output.writelines(key + '=' + shlex.quote(value) + '\n' for key, value in values.items())
   PY
   stat -c '%U:%G %a %n' /opt/atlas-src/stable.env
   ```

   Expected: prompts with hidden input, then `root:root 600`; no secret output.
   Do not copy development credentials or a development `.env` into the worktree.

4. **VM, root.** Generate the dedicated key for the web-server tunnel and install
   its restricted public key. This is a forward-only login with no terminal session.

   ```bash
   cd /opt/atlas-src
   id atlas-forward >/dev/null 2>&1 || useradd --create-home --shell /bin/sh atlas-forward
   install -d -o root -g root -m 700 /opt/atlas-src/stable-key-transfer
   ssh-keygen -q -t ed25519 -N '' -C atlas-indidea-tunnel -f /opt/atlas-src/stable-key-transfer/id_ed25519
   chmod 600 /opt/atlas-src/stable-key-transfer/id_ed25519
   chmod 644 /opt/atlas-src/stable-key-transfer/id_ed25519.pub
   install -d -o atlas-forward -g atlas-forward -m 700 /home/atlas-forward/.ssh
   { printf 'from="%s",restrict,port-forwarding,permitopen="127.0.0.1:3100",command="/bin/false" ' "$WEB_SERVER_IP"; cat /opt/atlas-src/stable-key-transfer/id_ed25519.pub; } >> /home/atlas-forward/.ssh/authorized_keys
   chown atlas-forward:atlas-forward /home/atlas-forward/.ssh/authorized_keys
   chmod 600 /home/atlas-forward/.ssh/authorized_keys
   cat > /etc/ssh/sshd_config.d/atlas-forward.conf <<'SSH'
   Match User atlas-forward
       AllowTcpForwarding local
       AllowAgentForwarding no
       X11Forwarding no
       PermitTTY no
       PermitTunnel no
       GatewayPorts no
   Match all
   SSH
   chown root:root /etc/ssh/sshd_config.d/atlas-forward.conf
   chmod 644 /etc/ssh/sshd_config.d/atlas-forward.conf
   sshd -t
   systemctl reload ssh
   ```

   Expected: no key material printed. Private key root:root 600, public key 644,
   transfer directory 700. SSH `-N` never requests the forced command. The public
   source IP and local-only forwarding restriction are explicit. SSH configuration
   is root:root 644. Expected: `sshd -t` succeeds and SSH reload succeeds; existing
   owner sessions remain open. No other forwarding destination is authorized.

5. **VM, root.** Record the reviewed CI tag and install the unit. Run the first deploy.

   ```bash
   cd /opt/atlas-src
   touch stable-ci-green-tags
   chown root:root stable-ci-green-tags
   chmod 600 stable-ci-green-tags
   printf '%s\n' "$TAG" >> stable-ci-green-tags
   install -o root -g root -m 644 stable-kit/atlas-stable.service /etc/systemd/system/atlas-stable.service
   systemctl daemon-reload
   systemctl enable atlas-stable.service
   unset ATLAS_STABLE ATLAS_PORT_BASE
   bash stable-kit/deploy.sh "$TAG"
   ```

   Expected: candidate local checks, detached `/opt/atlas-src/stable`, a `.venv`
   symlink to the shared runtime, service startup, successful health checks and
   `/opt/atlas-src/stable-last-deploy`. Worktrees and marker files belong to root;
   private directories are 700. Unit files are root:root 644. The deployment marker
   is metadata, not a substitute for GitHub CI. A health failure leaves the front
   door closed; use rollback in part C. First deployment has no preceding release.

## B. Web server front door

6. **Registrar account, owner; web server, owner's login with sudo.** Create DNS
   `A atlas` pointing to the **web server's** public IPv4 address, not the VM. Create
   `AAAA atlas` only if IPv6 reaches that same nginx server. Do not change the apex
   records. On the web server:

   ```bash
   export VM_HOST=VM_PUBLIC_IP
   export WEB_OWNER="$(id -un)"
   set +x
   umask 077
   sudo apt-get install -y apache2-utils certbot python3-certbot-nginx rsync
   sudo install -d -o "$WEB_OWNER" -g www-data -m 755 /var/www/indidea/atlas
   sudo install -d -o root -g root -m 700 /etc/atlas
   ```

   Expected: sudo prompts for the owner's password; existing nginx 1.18 remains in
   place. Web root owner:www-data 755; public HTML files will be 644. No Docker is
   needed here. From the VM as root, transfer the tag's public kit (using the
   public `WEB_OWNER` set in part A):

   ```bash
   rsync -a --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r /opt/atlas-src/stable-kit/ "$WEB_OWNER@indidea.org:atlas-kit/"
   ```

   Expected: `$HOME/atlas-kit` on the web server belongs to the owner, directories
   755, public files 644. Scripts are invoked with `bash`; executable mode is not
   needed for this transferred copy. No secret environment file is transferred.

7. **Web server, owner with sudo.** Bootstrap HTTP before requesting the certificate;
   the final TLS block cannot be loaded until its certificate exists.

   ```bash
   sudo tee /etc/nginx/sites-available/atlas.indidea.org >/dev/null <<'NGINX'
   server {
       listen 80;
       listen [::]:80;
       server_name atlas.indidea.org;
       root /var/www/indidea/atlas;
       location / { return 503; }
   }
   NGINX
   sudo chmod 644 /etc/nginx/sites-available/atlas.indidea.org
   sudo ln -s /etc/nginx/sites-available/atlas.indidea.org /etc/nginx/sites-enabled/atlas.indidea.org
   sudo nginx -t
   sudo systemctl reload nginx
   sudo certbot --nginx -d atlas.indidea.org
   ```

   Expected: DNS resolves correctly, nginx configuration succeeds and certbot
   creates the sub-domain certificate. Root owns the server block and symlink.
   Certbot owns certificate renewal and private-key permissions; keep its key
   root-only (600) and do not copy it to the web root.

8. **Web server, owner with sudo.** Create guests interactively. Use simple lowercase
   login names (letters, numbers, hyphens), unique per person.

   ```bash
   sudo htpasswd -c /etc/nginx/atlas.htpasswd alice
   sudo htpasswd /etc/nginx/atlas.htpasswd bob
   sudo chown root:www-data /etc/nginx/atlas.htpasswd
   sudo chmod 640 /etc/nginx/atlas.htpasswd
   ```

   Expected: hidden password prompts; file root:www-data 640. Use `-c` only once:
   it replaces the file. Adding a later guest uses `sudo htpasswd
   /etc/nginx/atlas.htpasswd carol`; removing one uses `sudo htpasswd -D
   /etc/nginx/atlas.htpasswd carol`. Neither operation deletes their stored history.

9. **VM, root, then web server, owner with sudo.** Transfer the tunnel private key
   and a secret include over authenticated SSH into a private staging directory.
   First, on the web server:

   ```bash
   install -d -m 700 "$HOME/atlas-private-transfer"
   ```

   Then on the VM, root (set `WEB_OWNER` to the real owner login):

   ```bash
   cd /opt/atlas-src
   scp stable-key-transfer/id_ed25519 "$WEB_OWNER@indidea.org:atlas-private-transfer/id_ed25519"
   python3 - <<'PY' | ssh "$WEB_OWNER@indidea.org" 'umask 077; cat > "$HOME/atlas-private-transfer/atlas-proxy-secret.conf"'
   import shlex
   from pathlib import Path
   values = dict(line.split('=', 1) for line in Path('stable.env').read_text().splitlines())
   secret = shlex.split(values['ATLAS_PROXY_SECRET'])[0]
   if not secret.isalnum():
       raise SystemExit('Expected generated hexadecimal proxy secret')
   print('proxy_set_header X-Atlas-Proxy-Secret "' + secret + '";')
   PY
   ```

   Expected: secret goes only through the encrypted pipe into an owner-only file,
   never to terminal output. Verify the SSH host fingerprint out of band before
   accepting it. Now on the web server, owner with sudo:

   ```bash
   sudo id atlas-tunnel >/dev/null 2>&1 || sudo useradd --system --create-home --home-dir /var/lib/atlas-tunnel --shell /usr/sbin/nologin atlas-tunnel
   sudo install -d -o atlas-tunnel -g atlas-tunnel -m 700 /var/lib/atlas-tunnel/.ssh
   sudo install -o atlas-tunnel -g atlas-tunnel -m 600 "$HOME/atlas-private-transfer/id_ed25519" /var/lib/atlas-tunnel/.ssh/id_ed25519
   sudo install -o root -g www-data -m 640 "$HOME/atlas-private-transfer/atlas-proxy-secret.conf" /etc/nginx/atlas-proxy-secret.conf
   rm "$HOME/atlas-private-transfer/id_ed25519" "$HOME/atlas-private-transfer/atlas-proxy-secret.conf"
   rmdir "$HOME/atlas-private-transfer"
   ```

   Expected: private key atlas-tunnel:atlas-tunnel 600; include root:www-data 640,
   outside both Git and the web root, containing exactly one `proxy_set_header`
   directive. After verifying the tunnel, remove the VM's transfer private key:
   `rm /opt/atlas-src/stable-key-transfer/id_ed25519` (VM root).

10. **Web server, owner with sudo.** Pin the VM SSH host key and install the tunnel.
    `ssh-keyscan` alone does not authenticate a host: compare its fingerprint with
    the VM's `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` (VM root) first.

    ```bash
    ssh-keyscan -t ed25519 "$VM_HOST" > "$HOME/atlas-vm-hostkey"
    ssh-keygen -lf "$HOME/atlas-vm-hostkey"
    # Continue only after the fingerprint matches the VM console.
    sudo install -o atlas-tunnel -g atlas-tunnel -m 600 "$HOME/atlas-vm-hostkey" /var/lib/atlas-tunnel/.ssh/known_hosts
    rm "$HOME/atlas-vm-hostkey"
    sudo tee /etc/atlas/indidea-tunnel.env >/dev/null <<ENV
    ATLAS_TUNNEL_KEY=/var/lib/atlas-tunnel/.ssh/id_ed25519
    ATLAS_TUNNEL_USER=atlas-forward
    ATLAS_TUNNEL_HOST=$VM_HOST
    ENV
    sudo chown root:root /etc/atlas/indidea-tunnel.env
    sudo chmod 600 /etc/atlas/indidea-tunnel.env
    sudo install -o root -g root -m 644 "$HOME/atlas-kit/atlas-indidea-tunnel.service" /etc/systemd/system/atlas-indidea-tunnel.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now atlas-indidea-tunnel.service
    ss -ltn 'sport = :3100'
    ```

    Expected: only `127.0.0.1:3100` on the web server; no external listener. The unit
    runs as atlas-tunnel, the environment file is root:root 600, unit 644. SSH uses
    `-L`, not a reverse forward, with strict host-key checks and keepalives.

11. **Web server, owner with sudo; static push from VM, owner.** Install the final
    nginx block after the secret, password file, tunnel and certificate exist.

    ```bash
    sudo install -o root -g root -m 644 "$HOME/atlas-kit/nginx/atlas.indidea.org.conf" /etc/nginx/sites-available/atlas.indidea.org
    sudo nginx -t
    sudo systemctl reload nginx
    ```

    Expected: syntax successful and reload successful. Every proxy location must
    retain its own secret include and identity/role replacement (nginx header
    inheritance is all-or-nothing). Never run `nginx -T`: it prints the include.
    The sites-enabled symlink from step 7 remains. Install the landing fragment
    inside the existing apex server block only after owner review; it serves the
    static `/atlas` page and is not a proxy.

    On the VM, as the owner's SSH-enabled account (the script targets
    `gael@indidea.org`; change that public destination in the reviewed script if
    the owner login differs):

    ```bash
    cd /opt/atlas-src/stable
    bash infra/stable/push-static.sh --dry-run
    bash infra/stable/push-static.sh
    ```

    Expected: rsync lists only landing/maintenance/static files, then copies them;
    no remote deletion. Destination directories owner:www-data 755, files 644.
    For a root-only release checkout, copy the public kit into the owner's home
    first and run its script there; never loosen private state permissions.

## C. Checks and operations

12. **VM, root; web server, owner.** Verify each hop without printing a secret.

    ```bash
    # VM, root
    /opt/atlas-src/stable/infra/stable/healthcheck.sh
    curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3100/
    ss -ltn | grep -E ':(3100|8100|8110|8111|8120)\b'
    # Web server, owner
    curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3100/
    curl -s -o /dev/null -w '%{http_code}\n' https://atlas.indidea.org/
    curl --user alice -s -o /dev/null -w '%{http_code}\n' https://atlas.indidea.org/
    curl --user alice -s -D - -o /dev/null https://atlas.indidea.org/ | grep -i x-robots-tag
    ```

    Expected: health succeeds, direct VM and tunneled unsigned requests return
    403, public unsigned request returns 401, authenticated request returns 200
    and `X-Robots-Tag: noindex, nofollow, noarchive`. Guest passwords are prompted.
    In a browser, verify streamed chat, PNG/PDF uploads, a presentation download,
    ordinary user role, and a second guest's empty history. The automated harness
    can rerun against this URL with synthetic guest accounts; its chat operations
    consume provider credit on a live deployment. Never run the disposable stack
    launcher or stop the tunnel from that harness on production.

13. **Web server, owner with sudo.** Add/remove a guest using step 8. Expected:
    new users appear automatically with the plain user role; removed Basic logins
    receive 401 even if they retain a UI token. Existing websocket connections may
    survive password removal: to revoke them immediately, stop/start the tunnel
    (affects all guests). Deleting data is a separate owner retention operation.

14. **VM, root.** Inspect health and aggregate spend, without printing conversations.

    ```bash
    systemctl --no-pager status atlas-stable.service
    /opt/atlas-src/stable-python/bin/python3 - <<'PY'
    import json
    from datetime import date
    from decimal import Decimal
    from pathlib import Path
    path = Path('/var/lib/atlas-stable/logs') / (date.today().isoformat() + '.jsonl')
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    print('requests:', len(rows))
    print('recorded EUR:', sum((Decimal(str(row.get('cout_eur', 0))) for row in rows), Decimal(0)))
    PY
    ```

    Expected: service active and numeric totals only. Provider invoices and search
    dashboards are authoritative; reserved/failed calls can differ from recorded
    cost. This kit does **not** implement guest-specific daily ledgers or a global
    daily adapter cap. nginx limits chat bursts by IP, not daily cost. The owner
    must resolve that design gap before broadening access; provider caps and alerts
    remain necessary. Existing per-request budgets and search limits still apply.

15. **Web server, owner with sudo; VM, root.** Kill switch and rollback:

    ```bash
    # Web server: cut access immediately; nginx now serves maintenance (502/504).
    sudo systemctl stop atlas-indidea-tunnel.service
    # VM: stop serving and inference.
    systemctl stop atlas-stable.service
    # VM: restore the retained preceding release, then verify it.
    bash /opt/atlas-src/stable/infra/stable/rollback.sh
    bash /opt/atlas-src/stable/infra/stable/healthcheck.sh
    # Web server: reopen only after health succeeds.
    sudo systemctl start atlas-indidea-tunnel.service
    ```

    Expected: maintenance while disconnected; previous tag after rollback. The
    script preserves data volumes; it is a **code rollback**, not a database schema
    downgrade. Take an owner-only backup of UI/terminal volumes and private state
    before upgrades; test restore separately. If there is no previous worktree,
    leave the service stopped and redeploy a reviewed tag. Provider-key revocation
    is the independent emergency spending switch.

16. **VM, root; web server, owner with sudo.** Rotate secrets during maintenance.
    Stop tunnel and stack as in step 15. On the VM:

    ```bash
    cd /opt/atlas-src
    python3 - <<'PY'
    import os, secrets
    from pathlib import Path
    path = Path('stable.env')
    lines = path.read_text().splitlines()
    rotate = {'WEBUI_SECRET_KEY', 'ATLAS_PROXY_SECRET', 'OPEN_TERMINAL_API_KEY'}
    with path.open('w') as output:
        for line in lines:
            key = line.split('=', 1)[0]
            output.write(key + '=' + secrets.token_hex(32) + '\n' if key in rotate else line + '\n')
    os.chmod(path, 0o600)
    PY
    ```

    Expected: no values printed, root:root 600 retained. Transfer only the new
    proxy include using step 9's private pipe and install command; the key pair
    need not change. On the web server run `sudo nginx -t` and reload nginx. On the
    VM start the stack, run health, then reopen the tunnel. Rotating the WebUI key
    invalidates guest sessions; expect fresh sign-in. Rotate provider keys through
    their dashboard, update the root-only environment privately, restart and revoke
    old credentials. To rotate the tunnel key, repeat steps 4/9/10, test the new
    key, then remove the old authorized_keys line and private key.

17. **VM, root; web server, owner with sudo.** Update to a new tag: repeat the exact
    CI review in step 1, backup private state, close the tunnel, append the reviewed
    tag to the root-only marker, and run `deploy.sh "$TAG"`. Candidate tests run
    with stable mode off on base 1900. Expected: new exact tag, healthy stack and
    retained previous worktree. Review nginx/kit changes separately; deploy static
    assets with dry-run first, then reopen the tunnel. Never promote a moving branch.

Guest content is stored on the VM: private logs are 700 directories/600 files,
host-side state databases inherit service umask 0077, and Docker volumes are accessible to
root/Docker administrators. The owner controls backups, retention, guest consent,
TLS/DNS/SSH updates, provider accounts and key revocation. The files service is a
shared container: the relay blocks guest execution, browsing and websocket terminal
access, but generated download URLs are capabilities, not per-guest ACLs;
do not share links or upload sensitive material. This PoC is for known guests,
not an untrusted multi-tenant service.

## After every deployment: look at the public page

Tests that call the adapter directly skip the front door. Run `PROBE_USER=... PROBE_PW=... python infra/stable/public_probe.py "<prompt>" <screenshot.png>` (Playwright, real browser, the public URL) and open the screenshot: images must render (`complete` true, non-zero size) and "Image unavailable" must not appear. A missing nginx route for `/images/` once made every generated image and SVG drawing look broken while the adapter served them correctly.
