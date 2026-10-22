# Try Atlas in under 10 minutes

Run a private trial on a Linux server with Docker Engine and the Compose plugin,
Python 3.10 or newer, Git, **4 vCPU, 8 GB RAM and 30 GB free disk**. Choose one inference
provider account, or local Ollama (a GPU is recommended for useful performance).
The CPU-only local profile starts without a GPU, but its small demonstration model
is substantially weaker. First downloads depend on your connection; ten minutes
is a target, not a guarantee.

## Choose an inference provider

The numbered setup menu follows this order. Presets, exact model IDs, endpoint
URLs and source dates live in [providers.yaml](../services/model-gateway/providers.yaml).
The sources below were checked on **2026-10-22**. Availability can change; setup
checks your account's model list and performs one small billed completion.

| Provider | Indicative input / output per million tokens | Notes and official sources |
| --- | --- | --- |
| Hetzner | Free during the experiment | **Experimental; not for production.** [API, models and terms](https://docs.hetzner.com/general/company-and-policy/experiments/inference/) |
| Scaleway | EUR 0.60 / 3.60 text; EUR 0.25 / 0.50 vision | Current Atlas defaults; [quickstart](https://www.scaleway.com/en/docs/generative-apis/quickstart/), [pricing](https://www.scaleway.com/en/pricing/model-as-a-service/), [FAQ](https://www.scaleway.com/en/docs/generative-apis/faq/) |
| Mistral | USD 0.15 / 0.60 | [Model](https://docs.mistral.ai/models/mistral-small-4-0-26-03), [API](https://docs.mistral.ai/api/endpoint/chat), [pricing](https://docs.mistral.ai/inference/pricing) |
| IONOS AI Model Hub | **Unverified price** | [API](https://docs.ionos.com/cloud/ai/ai-model-hub/how-tos/text-generation), [model](https://docs.ionos.com/cloud/ai/ai-model-hub/models/llms/qwen3-5-9b) |
| OVHcloud AI Endpoints | **Unverified price** | [Catalog](https://www.ovhcloud.com/en/public-cloud/ai-endpoints/catalog/), [API example](https://docs.ovhcloud.com/en/guides/public-cloud/ai-machine-learning/ai-endpoints-structured-output) |
| Local Ollama | No token charge; hardware and electricity remain | [API](https://docs.ollama.com/api/openai-compatibility), [model sizes](https://ollama.com/library/qwen3), [Docker](https://docs.ollama.com/docker) |
| Hugging Face Inference Providers | **Unverified, route-dependent price** | Outside the EU; router destination depends on the selected provider. [API](https://huggingface.co/docs/inference-providers/tasks/chat-completion), [billing](https://huggingface.co/docs/inference-providers/pricing) |
| DeepSeek | **Unverified price** | Outside the EU. [API](https://api-docs.deepseek.com/); the [pricing URL](https://api-docs.deepseek.com/quick_start/pricing) redirected to the quickstart during verification. |
| Other | **Unverified** until configured | Supply a base URL and model ID from your provider's documentation. |

The first five providers are European companies; check the selected service's
hosting region and terms. A provider's location alone does not establish the
location of every request. Ollama runs on your host.

Prices exclude tax and are estimates, not billing guarantees. **Without a verified
EUR price, Atlas disables the monetary budget ceiling and logs a warning.** Token,
time and tool limits remain active; provider billing still applies. A custom model
has no inherited price. For USD presets, set an operator-verified
`ATLAS_USD_TO_EUR` conversion in `.env` to enable EUR estimates. Unpriced usage
currently appears as zero in numeric cost telemetry; the warning distinguishes it
from free inference. Set spending limits in your provider account.

## Start

```bash
git clone https://github.com/leag1234/atlas.git
cd atlas
./atlas setup
```

Choose a provider, enter the API key at the hidden prompt, optionally override the
text model, and accept startup. Setup requires only Python's standard library and
Docker; it writes `.env` atomically with mode 600 and never displays the key. It
refuses to overwrite an existing `.env`. Keep that file private.

Open **http://localhost:13700**. For a remote server, run this on your workstation
(replace the login and host), then open the same URL:

```bash
ssh -N -L 13700:127.0.0.1:13700 user@your-server
```

Create the first administrator account in Open WebUI. The attachment inlet is
installed automatically within a few seconds after signup; refresh the page before
uploading your first document. Subsequent accounts require administrator approval.
Use the administration panel to manage users and disable signups when finished.
The trial listener binds only to loopback; public hosting is covered in the
[full stack installation](installation.md).

Automation reads the key from a private file, not a command-line argument:

```bash
./atlas setup --provider scaleway --api-key-file "$HOME/.config/atlas/provider-key" --yes
```

Add `--no-start` to validate and save remote settings without starting the stack.
For a custom endpoint, add `--base-url https://your-provider.example/v1 --model
YOUR_MODEL_ID`. Choosing `local` enables the Compose `local` profile and downloads
the preset model on first start. CPU-only startup is supported; a larger model and
GPU need more RAM and disk than the remote-inference target.

## What you get

Open WebUI provides accounts and conversation history. Atlas adds streaming chat,
PDF and office attachment reading, cited web answers through SearXNG, diagrams,
and retrieval backed by PostgreSQL. CPU embeddings and reranking run in the model
gateway; chat and supported vision calls go to your selected inference provider.
The default Scaleway preset preserves Atlas's separate text, code, fast and vision
roles. Other presets use their selected text model for all text profiles.

Try a short question, upload a small PDF and ask for its main points, ask a factual
web question with sources, then request a simple flow diagram. Scanned PDFs and
image understanding require an enabled vision model; not every preset provides one.
The retrieval index starts empty; attached documents are read directly.

The trial does **not** provide GPU image generation, Code mode, speech, a terminal
for generated office files, or HTTPS. Those features need the full installation.
Unavailable feature requests report that they are not enabled on the trial.

SearXNG relays queries to public engines and may be rate limited or return no
results. Self-hosting it does not keep search queries on your host. Answer quality
and tool compatibility depend on the chosen model; small local models are much
weaker. Citations show retrieved sources, not a guarantee of accuracy. The trial
is intended for evaluation, not hardened multi-organization hosting.

## Inspect, stop and remove

```bash
docker compose ps
docker compose logs --tail 50 gateway orchestrator searxng
docker compose stop
# Restart, retaining conversations, uploads and model downloads:
docker compose up -d --wait
# Remove the stack and ALL its saved data:
docker compose --profile local down --volumes --remove-orphans
```

The final command deletes this Compose project's database, conversations, uploads
and model cache. `.env` remains on disk. Avoid `docker compose config` in public
logs: its expanded output includes credentials. No cloud GPU is provisioned by
this Compose stack. Image publication is an owner-installed
[release template](../infra/release/ghcr-publish.yml); the quickstart builds from
repository Dockerfiles and does not depend on unpublished registry images.

For a small x86_64 CPU-only local installation, the Ollama Dockerfile also provides
an optional `cpu` build target. It verifies the official release checksum and
omits GPU libraries. Use the default `full` target when adding GPU access; the
trial never requests GPU devices automatically.
