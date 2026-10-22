# Full stack installation

Allow **30–60 minutes**, plus model downloads and GPU provisioning. For a first
look, use the [Docker quickstart](quickstart.md). The trial has fewer dependencies;
this guide covers the existing host launcher, document terminal, owner Code
preview, speech, an independently managed image GPU, HTTPS and operations.

## 1. Prepare the hosts

Use Linux with Python 3.12 or newer, Git, make, Docker Engine, curl, OpenSSH,
`nsenter` from util-linux, `iptables` and `ip6tables`. The application host needs
4 vCPU, 8 GB RAM and 30 GB free disk at minimum; allow additional RAM and disk for
speech models, uploads and backups. Docker access and root or noninteractive sudo
are required for terminal network containment. A containerized shell without
permission to enter network namespaces cannot run the full launcher.

For image generation, separately arrange a Linux NVIDIA GPU host with Docker and
the NVIDIA Container Toolkit, enough VRAM for the configured image model, and
storage for weights. Restrict the worker port to the application host. Set budget
alerts and a shutdown schedule with the cloud provider before enabling it. A domain
pointing at the application host and inbound TCP 80/443 are required for HTTPS.

Run on the application host:

```bash
git clone https://github.com/leag1234/atlas.git
cd atlas
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
export ATLAS_ROOT="$PWD"
export ATLAS_PORT_BASE=2100
mkdir -p "$HOME/.config/atlas" "$HOME/.local/share/atlas"
chmod 700 "$HOME/.config/atlas" "$HOME/.local/share/atlas"
```

Reuse `.venv` if it already exists. Use a separate checkout and private state for
each deployment. `ATLAS_PORT_BASE` is an **offset** in the host launcher, unlike
the Compose trial's explicit host port. At offset 2100, the UI is 5100, gateway
10110, retrieval 10111, adapter 10120, speech 10121/10122 and SearXNG 10988.
PostgreSQL uses a random loopback port. None of these application ports needs to
be public.

## 2. Configure inference, search and secrets

The full launcher retains its existing Scaleway configuration. The Compose trial
supports the additional [provider presets](../services/model-gateway/providers.yaml).
Obtain an account key and enable access to the models listed in
[routing.yaml](../services/model-gateway/routing.yaml). Configure account spending
caps. The following helper asks for credentials without displaying them and reads
model defaults from the gateway configuration:

```bash
export ATLAS_SECRETS_FILE="$HOME/.config/atlas/server.env"
python3 - <<'PY'
import getpass, os, secrets, shlex
from pathlib import Path
import yaml
path = Path(os.environ['ATLAS_SECRETS_FILE'])
if path.exists():
    raise SystemExit('Configuration already exists; edit it privately instead.')
routing = yaml.safe_load(Path('services/model-gateway/routing.yaml').read_text())
values = {
    'SCW_GENERATIVE_BASE_URL': 'https://api.scaleway.ai/v1',
    'SCW_GENERATIVE_API_KEY': getpass.getpass('Scaleway API key: '),
    'ESCALATION_MODEL': routing['serverless']['text']['model'],
    'OPEN_TERMINAL_API_KEY': secrets.token_urlsafe(32),
    'WEBUI_SECRET_KEY': secrets.token_urlsafe(32),
    'ATLAS_PORT_BASE': os.environ['ATLAS_PORT_BASE'],
    'ATLAS_SEARCH_PROVIDER': 'searxng',
    'ATLAS_DEV_GPU': '0',
    'ATLAS_CODE_MODE': '0',
    'ATLAS_TTS': '0',
    'ATLAS_STT': '0',
}
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as output:
    for name, value in values.items():
        output.write(name + '=' + shlex.quote(value) + '\n')
PY
```

The launcher loads `ATLAS_SECRETS_FILE`, then `.env` if present. A trial `.env`
overrides earlier variables: keep trial and full deployments in separate checkouts.
Never source an environment file supplied by an untrusted party. Never include
credentials in command history, logs or Git.

SearXNG starts automatically when selected. It needs no search key but sends queries
to public search engines and may be rate limited. Alternatively set
`ATLAS_SEARCH_PROVIDER=tavily` with `TAVILY_API_KEY`, or `serpapi` with `SERPAPI_KEY`.
Comma-separated provider names enable ordered fallback. Inference receives selected
conversation/document/image content; search providers receive search queries.
Public websites are fetched subject to Atlas's network and robots rules.

## 3. Start and verify the base services

```bash
source .venv/bin/activate
export ATLAS_SECRETS_FILE="$HOME/.config/atlas/server.env"
export ATLAS_PORT_BASE=2100
make serve
```

Wait for `ATLAS ready`. Keep this terminal open; Ctrl-C stops the host services and
launcher-owned containers while preserving persistent data. On your workstation:

```bash
ssh -N -L 5100:127.0.0.1:5100 -L 10120:127.0.0.1:10120 user@your-server
```

Open http://localhost:5100. The host launcher is an owner workspace with local UI
authentication disabled; it must remain behind the SSH tunnel or an authenticated
reverse proxy. Its default owner account is not a public signup service. The Docker
trial, by contrast, uses normal Open WebUI signup and its first account is admin.

In another application-host terminal:

```bash
curl --fail --silent http://127.0.0.1:10120/v1/models
curl --fail --silent --output /dev/null http://127.0.0.1:5100/
```

Send a short question, upload a small PDF, request a cited web answer and a diagram.
Confirm the answer content and links. These are billable inference calls. The
contained document terminal supports generated files; its private API key must
match the environment loaded by the launcher. A containment failure is a startup
failure: fix host privileges, never bypass the firewall.

## 4. Enable the owner Code preview

Stop the foreground launcher, then append the setting and restart:

```bash
printf '\nATLAS_CODE_MODE=1\n' >> "$ATLAS_SECRETS_FILE"
make serve
```

Code mode uses disposable Docker workspaces without network access, with a read-only
root filesystem and resource limits. Use the Code control in the owner UI to create
a workspace, upload a small project and request a bounded change. Review the diff
and use explicit confirmations for actions that require them. This preview is for
the private owner workspace; stable guest mode disables it. Do not expose Docker's
socket or the document terminal directly to users.

## 5. Enable speech

Stop the launcher. Install the optional speech dependencies and download the
configured local models. These commands select model names from repository config:

```bash
python3 -m pip install faster-whisper av
export ATLAS_STT_MODEL_DIR="$HOME/.local/share/atlas/whisper"
export ATLAS_TTS_MODEL_DIR="$HOME/.local/share/atlas/piper"
python3 - <<'PY'
import json, os
from pathlib import Path
from huggingface_hub import snapshot_download, hf_hub_download
config = json.loads(Path('services/stt/config.json').read_text())
snapshot_download(config['model_repository'], cache_dir=os.environ['ATLAS_STT_MODEL_DIR'])
voices = json.loads(Path('services/tts/voices.json').read_text())
root = Path(os.environ['ATLAS_TTS_MODEL_DIR'])
root.mkdir(parents=True, exist_ok=True)
for row in voices.values():
    voice = row['voice']
    locale, speaker, quality = voice.split('-')
    prefix = f'{locale[:2]}/{locale}/{speaker}/{quality}/{voice}'
    for suffix in ('.onnx', '.onnx.json'):
        source = hf_hub_download('rhasspy/piper-voices', prefix + suffix)
        (root / (voice + suffix)).write_bytes(Path(source).read_bytes())
PY
python3 - <<'PY'
import os, shlex
with open(os.environ['ATLAS_SECRETS_FILE'], 'a') as output:
    for key in ('ATLAS_STT_MODEL_DIR', 'ATLAS_TTS_MODEL_DIR'):
        output.write('\n' + key + '=' + shlex.quote(os.environ[key]))
    output.write('\nATLAS_STT=1\nATLAS_TTS=1\n')
PY
make serve
```

Use a fresh Whisper cache directory: the STT service expects exactly one model
snapshot. Piper needs both the `.onnx` and matching `.onnx.json` per configured
voice. Test dictation and read-aloud in the browser. Microphone access needs
localhost or HTTPS. This provides dictation and playback, not real-time voice.

## 6. Connect an image GPU

This procedure uses an independently provisioned worker and does not depend on a
particular cloud host layout or SSH key filename. Set these variables to your
actual GPU host, SSH identity and its verified hourly rate; the examples prompt
for the values rather than choosing a resource for you:

```bash
read -r -p 'GPU SSH login (user@host): ' GPU_SSH_HOST
read -r -p 'SSH private key path: ' GPU_SSH_KEY
read -r -p 'GPU address reachable from application host: ' GPU_ADDRESS
read -r -p 'GPU hourly cost in EUR: ' GPU_HOURLY_EUR
export GPU_SSH_HOST GPU_SSH_KEY GPU_ADDRESS GPU_HOURLY_EUR
ssh -i "$GPU_SSH_KEY" "$GPU_SSH_HOST" 'mkdir -p atlas-image-worker'
scp -i "$GPU_SSH_KEY" services/model-gateway/image_worker.py \
  services/model-gateway/image-model.json "$GPU_SSH_HOST:atlas-image-worker/"
ssh -i "$GPU_SSH_KEY" "$GPU_SSH_HOST" 'docker run -d --name atlas-imagegen \
  --gpus all -p 10100:8000 \
  -v atlas-image-weights:/root/.cache/huggingface \
  -v "$HOME/atlas-image-worker/image_worker.py:/app/image_worker.py:ro" \
  -v "$HOME/atlas-image-worker/image-model.json:/app/image-model.json:ro" \
  --entrypoint /bin/bash \
  pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime@sha256:417bd75df6365104c283ea4c1651fb3530d9eb5a4c2fafa51943cff2a94e6385 \
  -c "pip install --quiet transformers==4.55.2 diffusers==0.35.1 accelerate==1.10.1 sentencepiece==0.2.1 protobuf==6.32.0 && python /app/image_worker.py"'
curl --fail --silent "http://$GPU_ADDRESS:10100/health"
python3 - <<'PY'
import os, shlex
from decimal import Decimal
rate = Decimal(os.environ['GPU_HOURLY_EUR'])
if not rate.is_finite() or not 0 < rate <= 2:
    raise SystemExit('This installation requires an hourly rate above zero and at most EUR 2.')
with open(os.environ['ATLAS_SECRETS_FILE'], 'a') as output:
    for key, value in {
        'ATLAS_IMAGE_GPU_IP': os.environ['GPU_ADDRESS'],
        'ATLAS_IMAGE_GPU_EUR_H': str(rate),
    }.items():
        output.write('\n' + key + '=' + shlex.quote(value))
    output.write('\n')
PY
```

Port 10100 matches the host launcher's terminal offset of 2100; the worker itself
listens on 8000 inside its separate host. Restrict ingress before starting it.
Model loading can take several minutes; inspect `docker logs atlas-imagegen` on the
GPU host and retry the health check after loading. Restart the application launcher
and request one image, then confirm it reaches the browser.

This worker is externally managed: stopping Atlas does not stop its cloud instance.
Stop the container, then terminate or power off the rented instance through your
provider's control plane and confirm billing state:

```bash
ssh -i "$GPU_SSH_KEY" "$GPU_SSH_HOST" 'docker stop atlas-imagegen && docker rm atlas-imagegen'
```

The repository also contains a Scaleway-specific disposable lifecycle; review its
project, quota, SSH and budget configuration before adopting it. Keep it separate
from a generic public installation. Retained model volumes remain billable.

## 7. Add HTTPS and access control

For the private full owner workspace, put nginx with Basic authentication in front
of the loopback UI. On Debian/Ubuntu, install nginx, Certbot and the password helper.
Set a domain with its DNS A/AAAA records already pointing at this host:

```bash
read -r -p 'Atlas domain: ' ATLAS_DOMAIN
export ATLAS_DOMAIN
sudo apt-get update
sudo apt-get install -y nginx apache2-utils certbot python3-certbot-nginx
sudo htpasswd -c /etc/nginx/atlas-users owner
sudo tee /etc/nginx/sites-available/atlas >/dev/null <<EOF_NGINX
server {
    listen 80;
    server_name $ATLAS_DOMAIN;
    auth_basic "Atlas";
    auth_basic_user_file /etc/nginx/atlas-users;
    client_max_body_size 32m;
    location / {
        proxy_pass http://127.0.0.1:5100;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_buffering off;
        proxy_read_timeout 310s;
    }
}
EOF_NGINX
sudo ln -s /etc/nginx/sites-available/atlas /etc/nginx/sites-enabled/atlas
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d "$ATLAS_DOMAIN" --redirect
sudo certbot renew --dry-run
```

Only share this owner workspace with people permitted to access the same history
and files. Basic authentication does not create isolated Atlas accounts. For
separate guest histories, use Open WebUI authentication and the
[stable deployment architecture](../infra/stable/README.md): trusted user headers
must be supplied by the authenticated proxy, stripped from incoming requests, and
protected by `ATLAS_PROXY_SECRET`. That deployment disables Code mode and requires
explicit image and speech opt-ins. The stable scripts are a reference deployment,
not a universal installer; adapt their paths and domain before installation.

Set the public origin for generated links, then restart the host launcher:

```bash
printf '\nATLAS_PUBLIC_URL=https://%s\n' "$ATLAS_DOMAIN" >> "$ATLAS_SECRETS_FILE"
```

For the Docker
trial with HTTPS, use the same proxy with upstream port 13700 and keep normal
Open WebUI accounts enabled. Do not publish the gateway, retrieval, database or
terminal ports.

## 8. Administration, backups and upgrades

In the Docker UI, the first signup is administrator; manage subsequent accounts
through Admin Panel, approve only intended users and disable signup when done.
Keep the provider account's spending limits active. Stable guest deployments also
support `ATLAS_USER_DAILY_EUR`, `ATLAS_GLOBAL_DAILY_EUR`,
`ATLAS_USER_MONTHLY_EUR` and `ATLAS_GLOBAL_MONTHLY_EUR`; unset caps are disabled.
Configure retention and disk monitoring for documents, histories and logs.

For a consistent Compose backup, stop writes and archive its persistent volumes.
Run these from the same checkout and Compose project used for deployment:

```bash
export ATLAS_BACKUP="$HOME/atlas-backup-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -m 700 "$ATLAS_BACKUP"
docker compose stop
for volume in webui postgres state; do
  docker compose run --rm --no-deps --entrypoint tar \
    -v "atlas_${volume}:/backup-source:ro" \
    -v "$ATLAS_BACKUP:/backup-target" gateway \
    -czf "/backup-target/${volume}.tgz" -C /backup-source .
done
cp .env "$ATLAS_BACKUP/environment.env"
chmod 600 "$ATLAS_BACKUP/environment.env"
docker compose up -d --wait
```

Replace the `atlas_` volume prefix if you set a different Compose project name.
Encrypt backups and store a second copy off-host. Test restore into a separate
project before relying on them. Model caches can be downloaded again. For the host
launcher, stop it before archiving its worktree-local state and the volumes shown
by `docker volume ls`; names include `ATLAS_PORT_BASE`. Preserve the private
environment separately. Never copy a running PostgreSQL data directory.

Upgrade only after backup. In the deployment checkout, select a reviewed release
and rebuild; keep the previous release and backup for rollback:

```bash
git fetch --tags
read -r -p 'Reviewed release tag: ' ATLAS_RELEASE
git checkout --detach "$ATLAS_RELEASE"
docker compose build
docker compose up -d --wait
curl --fail --silent --output /dev/null http://127.0.0.1:13700/
```

For the host launcher, stop it, update the reviewed release, reinstall that release's
requirements into the existing `.venv`, and restart `make serve`. Check chat,
attachments, citations, file downloads and optional features. Database changes can
require restoring the matching backup when rolling back. Do not use `down
--volumes` as an upgrade command.

Source: [host launcher](../services/orchestrator/serving.py),
[ports](../packages/ports.py), [terminal containment](../services/orchestrator/terminal_stack.py),
[speech services](../services/stt/service.py),
[image worker](../services/model-gateway/image_worker.py),
[Compose startup semantics](https://docs.docker.com/compose/how-tos/startup-order/).
