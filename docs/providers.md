# Inference providers

The status below records the owner's acceptance tests on fresh Ubuntu VMs,
following the installation documentation literally and checking PDF questions,
plain chat, cited web answers and diagrams in a real browser on 2026-10-22/23.
These results establish those tested flows, not every possible capability.

| Provider | Text model | Vision model | Status |
| --- | --- | --- | --- |
| Hetzner | `Qwen/Qwen3.6-35B-A3B-FP8` | `Qwen/Qwen3.6-35B-A3B-FP8` | Verified 2026-10-23 |
| Mistral | `mistral-small-2603` | Not enabled by this preset | Verified 2026-10-23 |
| Scaleway | `qwen3.5-397b-a17b` | `gemma-4-26b-a4b-it` | Verified 2026-10-23 |
| OVHcloud | `Qwen3.5-397B-A17B` | `Qwen2.5-VL-72B-Instruct` | Verified 2026-10-23 |
| IONOS | `Qwen/Qwen3.5-9B` | `Qwen/Qwen3.5-9B` | In progress |

IONOS has not been tested because no account key was available. Hetzner remains
experimental and is not intended for production. Mistral's four acceptance cases
took approximately 15 seconds each; OVHcloud's replacement text model took
18–24 seconds each in the owner's tests. These timings are observations, not SLAs.

OVHcloud's earlier text preset failed plain-chat and diagram acceptance. The new
preset passed all four flows. Both OVHcloud model IDs were additionally confirmed
against the account's live `/v1/models` list on 2026-10-23, and a tiny image request
confirmed that the vision model correctly identified a red square. OVHcloud's
price remains unverified and its context limit remains a conservative Atlas limit.

The OVHcloud catalog also lists `stable-diffusion-xl-base-v10`. Image generation
through that service is a future option; it is not integrated or enabled here.

Use `./atlas setup --reconfigure` to switch an existing installation. Keep `.env`
and the same Compose project so generated secrets and persistent volumes stay
associated. See [Change provider](quickstart.md#change-provider).

Endpoint URLs, model IDs, indicative prices and official source links are maintained
in [providers.yaml](../services/model-gateway/providers.yaml). Setup verifies your
account's model list and a small streaming completion; this connection check is
separate from browser acceptance. Unverified EUR prices disable the monetary
ceiling, while token, time and tool limits remain active. Provider billing still
applies; see the [quickstart pricing notes](quickstart.md#choose-an-inference-provider).
