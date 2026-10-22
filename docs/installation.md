# Install and run Atlas

The default stack runs chat inference on Scaleway's on-demand models and document
retrieval on the application host's CPU. Images can start a separate hourly GPU.
The commands below are an operator procedure, not a claim of deployment acceptance.

## Prerequisites

Use a Linux host with Python 3.12, Git, make, Docker Engine, curl and SSH. Allow disk
space for Docker volumes and model caches. The launcher needs Docker access,
`nsenter` (util-linux), `iptables` and `ip6tables`, with root or noninteractive sudo
for terminal containment. Keep application listeners private.

From your checkout, reuse an existing virtual environment or create one:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
```

First startup can download CPU model weights and container images. Optional
speech needs additional local model files; see below.

## Private configuration

Put credentials in a private shell environment file with mode 600, outside Git,
and set `ATLAS_SECRETS_FILE` to its path. `make serve` loads that file, then a local
`.env` if present; `.env` overrides earlier values. Do not enable shell tracing or
print credentials. The table lists variable names only; obtain credentials through
your provider account and generate separate application secrets.

| Purpose | Environment variables |
| --- | --- |
| Required Scaleway connection and text role | `SCW_GENERATIVE_BASE_URL`, `SCW_GENERATIVE_API_KEY`, `ESCALATION_MODEL` |
| Optional role overrides | `CODE_MODEL`, `FAST_MODEL`, `VISION_MODEL`, `VISION_PREVIOUS_MODEL`, `EMBEDDING_SERVERLESS_MODEL` |
| Required file-service key | `OPEN_TERMINAL_API_KEY` |
| Search selection and credentials | `ATLAS_SEARCH_PROVIDER`, `TAVILY_API_KEY`, `SERPAPI_KEY` |
| Private environment and port separation | `ATLAS_SECRETS_FILE`, `ATLAS_PORT_BASE` |
| Optional local speech | `ATLAS_TTS`, `ATLAS_TTS_MODEL_DIR`, `ATLAS_STT`, `ATLAS_STT_MODEL_DIR` |
| Optional owner Code preview | `ATLAS_CODE_MODE` |
| Optional connectors | `ATLAS_MCP_CONFIG` |

For Scaleway, use your project's Generative API HTTPS base endpoint, including its
API version, as `SCW_GENERATIVE_BASE_URL`. Atlas appends `/chat/completions` to it;
do not include that suffix in the base. The code deliberately has no default
provider hostname. Enable access to the configured models in your Scaleway project
and configure account spending caps. Choose `ESCALATION_MODEL` and any overrides
from [routing.yaml](../services/model-gateway/routing.yaml): the gateway requires
matching capability and price entries, not an arbitrary model identifier.

The launcher defaults to Tavily. `ATLAS_SEARCH_PROVIDER` accepts `tavily`, `serpapi`
or `searxng`, including a comma-separated ordered fallback chain. Supply a key for
each external provider you intend to use. A chain containing SearXNG starts its
local sidecar automatically.

## Network access and data destinations

| Destination | Required access and purpose |
| --- | --- |
| Scaleway Generative API endpoint configured in `SCW_GENERATIVE_BASE_URL` | HTTPS to its host and `/chat/completions`; prompts, selected document content, images and OCR pages go to configured inference services. |
| `api.tavily.com/search` or `serpapi.com/search.json` | HTTPS for the chosen external search provider; search queries leave the application host. |
| Local SearXNG `/search` | Loopback HTTP on the SearXNG port; the sidecar reaches external search engines over HTTPS. |
| Retrieved public websites | HTTP/HTTPS for page retrieval and citations, subject to SSRF and robots rules. |
| Optional GPU instance | SSH for provisioning and HTTP to its image-worker port for health checks and generation; restrict ingress to the application host. |
| Installation and model registries | Package indexes, container registries and model downloads, including PyPI, the PyTorch CPU index, GHCR, Docker Hub and Hugging Face. |

Tavily and SerpAPI are US companies, a known gap with the sovereignty goal.
**SearXNG is the self-hosted alternative.** The repository starts a pinned,
loopback-only Docker sidecar and queries its JSON API for general/news searches,
with caching and two bounded engine groups. Its current upstreams include
DuckDuckGo, Brave, Wikipedia, Qwant, Bing and Google: self-hosting the search
frontend does not keep upstream queries local. The client targets the local
sidecar, with no configurable remote SearXNG URL. See
[settings](../infra/searxng/settings.yml) and [client code](../services/orchestrator/tools.py).

## Start, check and stop

Choose an unused offset before launch. The example below uses 2100; put the same
setting in your private configuration so it survives restarts:

```bash
export ATLAS_PORT_BASE=2100
make serve
```

Wait for `ATLAS ready`. With this offset the UI is on 5100, the terminal on 10100,
the gateway on 10110, retrieval on 10111, the chat adapter on 10120 and SearXNG
on 10988 if enabled. PostgreSQL gets a random loopback port. The offset also
separates Docker container, network and volume names; use a separate checkout and
private state for each instance.

From a workstation, tunnel the UI and adapter:

```bash
ssh -N -L 5100:127.0.0.1:5100 -L 10120:127.0.0.1:10120 user@your-vm
```

Open `http://localhost:5100`. In another terminal on the application host, these
readiness checks do not request inference:

```bash
curl --fail --silent http://127.0.0.1:10120/v1/models
curl --fail --silent --output /dev/null http://127.0.0.1:5100/
```

The models list should contain `atlas-qwen`, `atlas-glm` and `atlas-deepseek`.
For functional acceptance, select a profile and send a short message, upload a
small PDF, then request a cited web answer. These actions consume inference/search
credit. Confirm useful content and working source links, not only an HTTP success.
Test image generation separately after configuring its GPU budget.

Ctrl-C stops the foreground stack while preserving UI, terminal and database
volumes. Starting `make serve` again replaces the launcher owned by that checkout;
coordinate restarts with its users. GPU cleanup has a separate lifecycle below.

## Optional image GPU

Prepare the Scaleway CLI with credentials limited to the dedicated project,
available GPU quota, spending alerts and the SSH key pair expected by
[infra/imagegen.sh](../infra/imagegen.sh), currently `/root/.ssh/atlas_m1` and its
public key. That script currently discovers exactly one BASIC application VM in
the project to determine the allowed client IP; review this deployment assumption
before using it on a different host layout.

Required variable names are `SCW_DEFAULT_PROJECT_ID`, `SCW_DEFAULT_ZONE`,
`LOCAL_MODEL` and `GPU_MAX_EUR_H`, in addition to the chat configuration.
`LOCAL_MODEL` is required by configuration validation even for the image workload;
keep model selection in the gateway. The launcher supplies `GPU_CLIENT_IP` and
`GPU_SSH_PUBLIC_KEY`. Keep the hourly ceiling at or below EUR 2 and the image-start
budget within EUR 30. Retained weight storage remains billable after shutdown.

`make serve` enables automatic startup when an owner requests an image. For an
explicit window, load the same private environment in a second shell, keep the
same port offset, then run:

```bash
make serve-imagegen
```

This provisions a GPU, loads the image worker and keeps it available for ten
minutes after readiness, then tears down the instance. Capacity shortages or model
loading can delay or prevent startup. The host must reach Scaleway's control plane,
the instance over SSH, and its image-worker port (10100 with the example offset).
The GPU also needs container, package and model download access.

The launcher traps exit signals for cleanup. To stop an allocated GPU explicitly,
with the same project/zone environment loaded:

```bash
bash infra/gpu-down.sh
```

Confirm termination in the dedicated project's resource inventory; stopping chat
alone is not evidence of GPU termination. Weight volumes can remain for reuse.

## Optional speech

Enable `ATLAS_TTS` for read-aloud and `ATLAS_STT` for dictation with the switch value
`1`. Piper is in the base requirements. Put each configured voice's `.onnx` and
matching `.onnx.json` files in `ATLAS_TTS_MODEL_DIR`; voice names come from
[voices.json](../services/tts/voices.json).

Dictation additionally needs `faster-whisper` and PyAV, which are not pinned in the
base requirements. Install them in the serving environment and stage the model
from [STT config](../services/stt/config.json) under `ATLAS_STT_MODEL_DIR`, using the
Hugging Face cache layout with exactly one snapshot. The service loads local files
only. Restart the stack and check the microphone and read-aloud controls; browser
microphone access needs localhost or HTTPS. This is dictation and playback, not
real-time voice conversation.

## Optional guest instance

Use the [stable deployment kit](../infra/stable/README.md) and its
[owner runbook](../infra/stable/RUNBOOK.md) on dedicated deployment paths. It provides
systemd startup, separate persistent state, a restricted SSH tunnel, nginx TLS and
Basic authentication, health checks and rollback. Its scripts contain owner-specific
paths and hostnames; review those before deployment. Deploy a reviewed tag whose
commit has green GitHub CI.

Two corrections apply to the older runbook: use this checkout's
`requirements-dev.txt` for runtime installation, and configure the daily/monthly
budgets now implemented in [daily_budget.py](../services/orchestrator/daily_budget.py).

In addition to provider configuration, set `ATLAS_STABLE`, `ATLAS_PORT_BASE`,
`WEBUI_SECRET_KEY`, `ATLAS_TRUSTED_EMAIL_HEADER`, `ATLAS_PROXY_SECRET`,
`ATLAS_HEALTHCHECK_USER` and `ATLAS_PUBLIC_URL`. Stable mode requires the trusted
email header name `X-Atlas-User`. Keep its environment and state separate.
Configure `ATLAS_USER_DAILY_EUR`, `ATLAS_GLOBAL_DAILY_EUR`,
`ATLAS_USER_MONTHLY_EUR`, `ATLAS_GLOBAL_MONTHLY_EUR` and the search caps before
admitting guests; unset cost caps are disabled. `ATLAS_STABLE_IMAGES` and
`ATLAS_STABLE_STT` are explicit opt-ins. Code mode is disabled for guests.

After installing the kit, start/stop the VM service with
`systemctl start atlas-stable.service` / `systemctl stop atlas-stable.service`.
Control the web-server tunnel with `systemctl start atlas-indidea-tunnel.service`
/ `systemctl stop atlas-indidea-tunnel.service`. Run the deployment's
`infra/stable/healthcheck.sh --ready` for readiness; without `--ready` it also makes
a billed inference call. Public access should return 401 without credentials and
200 after authentication. Check uploads, downloads and separate guest histories
in the browser. Preserve data volumes and back up private state before upgrades.

Source: [launcher](../services/orchestrator/serving.py),
[environment loader](../scripts/serve.sh), [ports](../packages/ports.py),
[configuration validation](../packages/configuration.py) and
[image lifecycle](../services/model-gateway/image_lifecycle.py).
